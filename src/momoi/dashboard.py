import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import re
import time
from importlib.metadata import PackageNotFoundError, version as package_version
from importlib.resources import files
from pathlib import Path

from aiohttp import web

from .emotions import (
    managed_emotion_bytes,
    remove_unreferenced_emotion_asset,
    valid_emotion_slug,
)
from .extensions.base import UsagePlugin
from .logging_context import log_event
from .storage import Store


logger = logging.getLogger(__name__)
ASSET_ROOT = files("momoi").joinpath("dashboard")
DASHBOARD_TOKEN = web.AppKey("dashboard_token", str)
USAGE_PLUGIN = web.AppKey("usage_plugin", UsagePlugin | None)
JWT_TTL_SECONDS = 365 * 24 * 60 * 60
JWT_SUBJECT = "momoi-dashboard"
_PUBLIC_ASSET_PATH = re.compile(r"^/api/emotions/[^/]+/asset$")
_AUTH_TOKEN_PATH = "/api/auth/token"


def momoi_version() -> str:
    try:
        return package_version("momoi")
    except PackageNotFoundError:
        return "0.0.0"


_ROOT_STATIC = {
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
    "favicon.ico": ("favicon.svg", "image/svg+xml"),
    "manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "apple-touch-icon-v2.png": ("apple-touch-icon.png", "image/png"),
    "icon-192.png": ("icon-192.png", "image/png"),
    "icon-192-v2.png": ("icon-192.png", "image/png"),
    "icon-512.png": ("icon-512.png", "image/png"),
    "icon-512-v2.png": ("icon-512.png", "image/png"),
}


def _bounded_int(
    request: web.Request, name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(request.query.get(name, default))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"invalid {name}") from None
    return min(maximum, max(minimum, value))


_LOCAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _optional_local_date(request: web.Request, name: str) -> str | None:
    raw = str(request.query.get(name) or "").strip()
    if not raw:
        return None
    if not _LOCAL_DATE.fullmatch(raw):
        raise web.HTTPBadRequest(text=f"invalid {name}")
    return raw


def _bearer_token(request: web.Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return ""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _token_matches(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False


def issue_dashboard_jwt(
    secret: str,
    *,
    ttl_seconds: int = JWT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    if not secret:
        raise ValueError("dashboard token is required")
    issued_at = int(time.time() if now is None else now)
    header = _b64url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    payload = _b64url_encode(
        json.dumps(
            {
                "sub": JWT_SUBJECT,
                "iat": issued_at,
                "exp": issued_at + max(1, int(ttl_seconds)),
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64url_encode(
        hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


def verify_dashboard_jwt(
    token: str, secret: str, *, now: int | None = None
) -> bool:
    if not token or not secret or token.count(".") != 2:
        return False
    header_segment, payload_segment, signature_segment = token.split(".", 2)
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected = _b64url_encode(
        hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature_segment, expected):
        return False
    try:
        header = json.loads(_b64url_decode(header_segment))
        payload = json.loads(_b64url_decode(payload_segment))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return False
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        return False
    if payload.get("sub") != JWT_SUBJECT:
        return False
    try:
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    return expires_at > current


def _require_token(request: web.Request) -> None:
    secret = str(request.app[DASHBOARD_TOKEN] or "")
    if not verify_dashboard_jwt(_bearer_token(request), secret):
        raise web.HTTPUnauthorized(
            text="unauthorized",
            headers={"WWW-Authenticate": 'Bearer realm="momoi-dashboard"'},
        )


def _is_public_api(request: web.Request) -> bool:
    path = request.path
    if _PUBLIC_ASSET_PATH.fullmatch(path):
        return True
    return request.method == "POST" and path == _AUTH_TOKEN_PATH


async def _json_body(request: web.Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as error:
        raise web.HTTPBadRequest(text="invalid json") from error
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="json object required")
    return payload


def _public_emotion(item: dict[str, object]) -> dict[str, object]:
    public = {key: value for key, value in item.items() if key != "path"}
    public["asset_url"] = f"/api/emotions/{item['slug']}/asset"
    return public


def _dashboard_recall(store: Store, turn_id: str) -> dict[str, object] | None:
    record = store.context_plan(turn_id)
    if record is None:
        return None
    plan = record.get("plan")
    retrieval = record.get("retrieval")
    if (
        not isinstance(plan, dict)
        or plan.get("version") != 7
        or not isinstance(retrieval, dict)
    ):
        return None
    units = [
        {
            "id": str(unit.get("id") or ""),
            "intent": str(unit.get("intent") or ""),
            "mode": str(unit.get("recall_mode") or ""),
            "queries": [
                {
                    "semantic": str(query.get("semantic") or ""),
                    "keywords": [
                        str(keyword) for keyword in query.get("keywords") or []
                    ],
                }
                for query in unit.get("recall_queries") or []
                if isinstance(query, dict)
            ],
            "reused_from": str(unit.get("recall_from_turn_id") or ""),
        }
        for unit in plan.get("intent_units") or []
        if isinstance(unit, dict)
    ]
    episodes: list[dict[str, object]] = []
    seen_episodes: set[str] = set()
    for selected in retrieval.get("episodes") or []:
        if not isinstance(selected, dict):
            continue
        episode_id = str(selected.get("episode_id") or "")
        if not episode_id or episode_id in seen_episodes:
            continue
        seen_episodes.add(episode_id)
        episode = store.episode(episode_id) or {}
        episodes.append(
            {
                "id": episode_id,
                "title": str(episode.get("title") or ""),
                "relation": str(selected.get("relation") or ""),
                "summary": str(
                    episode.get("narrative_summary")
                    or episode.get("working_summary")
                    or ""
                ),
            }
        )
    semantic = retrieval.get("semantic_recall")
    return {
        "revision": int(record.get("revision") or 0),
        "state": str(record.get("state") or ""),
        "units": units,
        "episode_actions": [
            action
            for action in plan.get("episode_actions") or []
            if isinstance(action, dict)
        ],
        "status": str(retrieval.get("query_recall") or ""),
        "memories": [
            item
            for item in retrieval.get("recall_memories") or []
            if isinstance(item, dict)
        ],
        "reflections": [
            item
            for item in retrieval.get("reflection_memories") or []
            if isinstance(item, dict)
        ],
        "episodes": episodes,
        "semantic": semantic if isinstance(semantic, dict) else {},
    }


@web.middleware
async def _auth(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    if request.path.startswith("/api/") and not _is_public_api(request):
        _require_token(request)
    return await handler(request)


@web.middleware
async def _headers(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    response = await handler(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def create_dashboard_app(
    store: Store, *, token: str = "", usage_plugin: UsagePlugin | None = None
) -> web.Application:
    app = web.Application(middlewares=[_auth, _headers])
    app[DASHBOARD_TOKEN] = token
    app[USAGE_PLUGIN] = usage_plugin
    workspace = store._workspace

    async def index(_request: web.Request) -> web.Response:
        page = ASSET_ROOT.joinpath("index.html")
        if not page.is_file():
            raise web.HTTPServiceUnavailable(text="dashboard frontend is not built")
        return web.Response(
            body=page.read_bytes(),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    async def asset(request: web.Request) -> web.Response:
        name = request.match_info["path"]
        if not name or ".." in name.split("/"):
            raise web.HTTPNotFound()
        resource = ASSET_ROOT.joinpath("assets", *name.split("/"))
        if not resource.is_file():
            raise web.HTTPNotFound()
        content_type, _ = mimetypes.guess_type(name)
        return web.Response(
            body=resource.read_bytes(),
            content_type=content_type or "application/octet-stream",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def root_static(request: web.Request) -> web.Response:
        spec = _ROOT_STATIC.get(request.match_info["name"])
        if spec is None:
            raise web.HTTPNotFound()
        filename, content_type = spec
        resource = ASSET_ROOT.joinpath(filename)
        if not resource.is_file():
            raise web.HTTPNotFound()
        return web.Response(
            body=resource.read_bytes(),
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": momoi_version()})

    async def issue_token(request: web.Request) -> web.Response:
        secret = str(request.app[DASHBOARD_TOKEN] or "")
        payload = await _json_body(request)
        provided = str(payload.get("token") or "").strip()
        if not _token_matches(provided, secret):
            raise web.HTTPUnauthorized(
                text="unauthorized",
                headers={"WWW-Authenticate": 'Bearer realm="momoi-dashboard"'},
            )
        return web.json_response(
            {
                "token": issue_dashboard_jwt(secret),
                "token_type": "Bearer",
                "expires_in": JWT_TTL_SECONDS,
            }
        )

    async def overview(request: web.Request) -> web.Response:
        data = store.dashboard_overview()
        plugin = request.app[USAGE_PLUGIN]
        if plugin is not None:
            data["balance"] = await plugin.balance()
        else:
            data["balance"] = {
                "source": "unavailable",
                "currency": "CNY",
                "is_available": False,
                "total_balance": "0",
            }
        return web.json_response(data)

    async def usage(request: web.Request) -> web.Response:
        days = _bounded_int(request, "days", 30, 1, 366)
        return web.json_response(store.dashboard_usage(days=days))

    async def thinking(request: web.Request) -> web.Response:
        try:
            return web.json_response(
                store.dashboard_thinking(
                    month=str(request.query.get("month") or ""),
                    limit=_bounded_int(request, "limit", 64, 1, 200),
                    cursor=_bounded_int(request, "cursor", 0, 0, 100_000),
                )
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None

    async def thinking_turn(request: web.Request) -> web.Response:
        turn_id = request.match_info["turn_id"]
        item = store.read_thinking(
            turn_id,
            str(request.query.get("call_id") or "").strip(),
        )
        if not item.get("ok"):
            raise web.HTTPNotFound(text="thinking not found")
        payload = {
            "ok": True,
            "items": item.get("calls") or [],
            "count": item.get("count") or 0,
        }
        episode = store.episodes_for_turns([turn_id]).get(turn_id)
        if episode:
            payload.update(episode)
        recall = _dashboard_recall(store, turn_id)
        if recall is not None:
            payload["recall"] = recall
        return web.json_response(payload)

    async def thinking_call(request: web.Request) -> web.Response:
        item = store.read_thinking("", request.match_info["call_id"])
        if not item.get("ok"):
            raise web.HTTPNotFound(text="thinking not found")
        calls = item.get("calls") or []
        if not calls:
            raise web.HTTPNotFound(text="thinking not found")
        return web.json_response({"ok": True, "item": calls[0]})

    async def conversations(request: web.Request) -> web.Response:
        limit = _bounded_int(request, "limit", 64, 1, 200)
        return web.json_response({"items": store.list_dashboard_conversations(limit)})

    async def conversation(request: web.Request) -> web.Response:
        token_budget = _bounded_int(request, "token_budget", 100_000, 1_000, 250_000)
        before = request.query.get("before_ordinal")
        try:
            before_ordinal = int(before) if before else None
        except ValueError:
            raise web.HTTPBadRequest(text="invalid before_ordinal") from None
        item = store.conversation_episode(
            request.match_info["episode_id"],
            token_budget,
            before_ordinal=before_ordinal,
        )
        if item is None:
            raise web.HTTPNotFound(text="conversation not found")
        return web.json_response(item)

    async def reflections(request: web.Request) -> web.Response:
        return web.json_response(
            store.list_reflections(
                _bounded_int(request, "limit", 14, 1, 90),
                before=_optional_local_date(request, "cursor"),
            )
        )

    async def memories(request: web.Request) -> web.Response:
        limit = _bounded_int(request, "limit", 200, 1, 500)
        return web.json_response({"items": store.list_memories(limit)})

    async def update_memory(request: web.Request) -> web.Response:
        try:
            memory_id = int(request.match_info["memory_id"])
        except ValueError:
            raise web.HTTPBadRequest(text="invalid memory id") from None
        payload = await _json_body(request)
        if "content" not in payload:
            raise web.HTTPBadRequest(text="content is required")
        try:
            item = store.update_memory_content(memory_id, str(payload["content"]))
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if item is None:
            raise web.HTTPNotFound(text="memory not found")
        return web.json_response(item)

    async def delete_memory(request: web.Request) -> web.Response:
        try:
            memory_id = int(request.match_info["memory_id"])
        except ValueError:
            raise web.HTTPBadRequest(text="invalid memory id") from None
        reason = request.query.get("reason") or "Deleted from dashboard"
        try:
            forgotten = store.forget_memory_by_id(memory_id, reason)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if not forgotten:
            raise web.HTTPNotFound(text="memory not found")
        return web.json_response({"ok": True})

    async def goals(request: web.Request) -> web.Response:
        include_closed = request.query.get("all", "").lower() in {
            "1",
            "true",
            "yes",
        }
        return web.json_response(
            {"items": store.list_goals(include_closed=include_closed)}
        )

    async def update_goal(request: web.Request) -> web.Response:
        payload = await _json_body(request)
        fields = {
            name: str(payload[name])
            for name in (
                "title",
                "success_criteria",
                "next_action",
                "status",
                "waiting_for",
                "blocked_reason",
            )
            if name in payload
        }
        if not fields:
            raise web.HTTPBadRequest(text="no updatable fields provided")
        try:
            item = store.update_goal_owner(request.match_info["goal_id"], **fields)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if item is None:
            raise web.HTTPNotFound(text="goal not found")
        return web.json_response(item)

    async def delete_goal(request: web.Request) -> web.Response:
        reason = request.query.get("reason")
        if reason is None and request.can_read_body:
            try:
                payload = await request.json()
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "reason" in payload:
                reason = str(payload["reason"])
        reason = reason or "Cancelled from dashboard"
        try:
            item = store.cancel_goal(request.match_info["goal_id"], reason)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if item is None:
            raise web.HTTPNotFound(text="goal not found")
        return web.json_response(item)

    async def emotions(_request: web.Request) -> web.Response:
        items = [_public_emotion(item) for item in store.list_emotions()]
        return web.json_response({"items": items})

    async def emotion_asset(request: web.Request) -> web.StreamResponse:
        slug = request.match_info["slug"]
        if not valid_emotion_slug(slug):
            raise web.HTTPNotFound()
        item = store.emotion(slug)
        if item is None:
            raise web.HTTPNotFound()
        path = Path(str(item["path"]))
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(
            path,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    async def _read_emotion_multipart(
        request: web.Request, *, require_file: bool
    ) -> tuple[str | None, str | None, Path | None]:
        reader = await request.multipart()
        slug: str | None = None
        description: str | None = None
        managed: Path | None = None
        async for part in reader:
            name = part.name or ""
            if name == "slug":
                slug = (await part.text()).strip()
            elif name == "description":
                description = (await part.text()).strip()
            elif name == "file":
                data = await part.read(decode=False)
                filename = part.filename or "emotion.bin"
                try:
                    managed = managed_emotion_bytes(workspace, data, filename)
                except ValueError as error:
                    raise web.HTTPBadRequest(text=str(error)) from None
        if require_file and managed is None:
            raise web.HTTPBadRequest(text="file is required")
        return slug, description, managed

    async def create_emotion(request: web.Request) -> web.Response:
        content_type = request.content_type or ""
        if "multipart/" not in content_type:
            raise web.HTTPBadRequest(text="multipart form required")
        slug, description, managed = await _read_emotion_multipart(
            request, require_file=True
        )
        if not slug or not valid_emotion_slug(slug):
            raise web.HTTPBadRequest(text="invalid slug")
        if not description:
            raise web.HTTPBadRequest(text="description is required")
        assert managed is not None
        previous = store.emotion(slug)
        try:
            item = store.add_emotion(slug, managed, description)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if previous and previous["path"] != item["path"]:
            remove_unreferenced_emotion_asset(store, str(previous["path"]), workspace)
        return web.json_response(_public_emotion(item), status=201)

    async def update_emotion(request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        if not valid_emotion_slug(slug):
            raise web.HTTPNotFound()
        existing = store.emotion(slug)
        if existing is None:
            raise web.HTTPNotFound(text="emotion not found")
        content_type = request.content_type or ""
        description: str | None = None
        managed: Path | None = None
        if "multipart/" in content_type:
            _, description, managed = await _read_emotion_multipart(
                request, require_file=False
            )
        else:
            payload = await _json_body(request)
            if "description" in payload:
                description = str(payload["description"]).strip()
        if description is None and managed is None:
            raise web.HTTPBadRequest(text="description or file is required")
        path = managed if managed is not None else Path(str(existing["path"]))
        desc = description if description is not None else str(existing["description"])
        try:
            item = store.add_emotion(slug, path, desc)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if managed is not None and existing["path"] != item["path"]:
            remove_unreferenced_emotion_asset(store, str(existing["path"]), workspace)
        return web.json_response(_public_emotion(item))

    async def delete_emotion(request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        if not valid_emotion_slug(slug):
            raise web.HTTPNotFound()
        item = store.emotion(slug)
        if item is None:
            raise web.HTTPNotFound(text="emotion not found")
        referenced = store.emotion_path_referenced(str(item["path"]), exclude_slug=slug)
        if not store.delete_emotion(slug):
            raise web.HTTPNotFound(text="emotion not found")
        if not referenced:
            remove_unreferenced_emotion_asset(store, str(item["path"]), workspace)
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/assets/{path:.+}", asset)
    app.router.add_get(
        "/{name:" + "|".join(re.escape(name) for name in _ROOT_STATIC) + "}",
        root_static,
    )
    app.router.add_post("/api/auth/token", issue_token)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/thinking", thinking)
    app.router.add_get("/api/thinking/calls/{call_id}", thinking_call)
    app.router.add_get("/api/thinking/{turn_id}", thinking_turn)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_get("/api/conversations/{episode_id}", conversation)
    app.router.add_get("/api/reflections", reflections)
    app.router.add_get("/api/memories", memories)
    app.router.add_patch("/api/memories/{memory_id}", update_memory)
    app.router.add_delete("/api/memories/{memory_id}", delete_memory)
    app.router.add_get("/api/goals", goals)
    app.router.add_patch("/api/goals/{goal_id}", update_goal)
    app.router.add_delete("/api/goals/{goal_id}", delete_goal)
    app.router.add_get("/api/emotions", emotions)
    app.router.add_post("/api/emotions", create_emotion)
    app.router.add_patch("/api/emotions/{slug}", update_emotion)
    app.router.add_delete("/api/emotions/{slug}", delete_emotion)
    app.router.add_get("/api/emotions/{slug}/asset", emotion_asset)
    return app


class DashboardService:
    def __init__(
        self,
        store: Store,
        host: str = "0.0.0.0",
        port: int = 8788,
        *,
        token: str = "",
        usage_plugin: UsagePlugin | None = None,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.token = token
        self.usage_plugin = usage_plugin

    async def run(self, stop: asyncio.Event) -> None:
        runner = web.AppRunner(
            create_dashboard_app(
                self.store, token=self.token, usage_plugin=self.usage_plugin
            ),
            access_log=None,
        )
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
            log_event(
                logger,
                logging.INFO,
                "dashboard_start",
                host=self.host,
                port=self.port,
            )
            await stop.wait()
        finally:
            await runner.cleanup()
