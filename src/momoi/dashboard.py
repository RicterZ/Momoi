import asyncio
import logging
import mimetypes
from importlib.resources import files
from pathlib import Path

from aiohttp import web

from .emotions import valid_emotion_slug
from .logging_context import log_event
from .storage import Store


logger = logging.getLogger(__name__)
ASSET_ROOT = files("momoi").joinpath("dashboard")


def _bounded_int(
    request: web.Request, name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(request.query.get(name, default))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"invalid {name}") from None
    return min(maximum, max(minimum, value))


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


def create_dashboard_app(store: Store) -> web.Application:
    app = web.Application(middlewares=[_headers])

    async def index(_request: web.Request) -> web.Response:
        return web.Response(
            body=ASSET_ROOT.joinpath("index.html").read_bytes(),
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

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def overview(_request: web.Request) -> web.Response:
        return web.json_response(store.dashboard_overview())

    async def conversations(request: web.Request) -> web.Response:
        limit = _bounded_int(request, "limit", 64, 1, 200)
        return web.json_response({"items": store.list_episode_directory(limit)})

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
        limit = _bounded_int(request, "limit", 90, 1, 366)
        return web.json_response({"items": store.list_reflections(limit)})

    async def memories(request: web.Request) -> web.Response:
        limit = _bounded_int(request, "limit", 200, 1, 500)
        return web.json_response({"items": store.list_memories(limit)})

    async def goals(request: web.Request) -> web.Response:
        include_closed = request.query.get("all", "").lower() in {
            "1",
            "true",
            "yes",
        }
        return web.json_response(
            {"items": store.list_goals(include_closed=include_closed)}
        )

    async def emotions(_request: web.Request) -> web.Response:
        items = []
        for item in store.list_emotions():
            public = {key: value for key, value in item.items() if key != "path"}
            public["asset_url"] = f"/api/emotions/{item['slug']}/asset"
            items.append(public)
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

    app.router.add_get("/", index)
    app.router.add_get("/assets/{path:.+}", asset)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_get("/api/conversations/{episode_id}", conversation)
    app.router.add_get("/api/reflections", reflections)
    app.router.add_get("/api/memories", memories)
    app.router.add_get("/api/goals", goals)
    app.router.add_get("/api/emotions", emotions)
    app.router.add_get("/api/emotions/{slug}/asset", emotion_asset)
    return app


class DashboardService:
    def __init__(
        self,
        store: Store,
        host: str = "0.0.0.0",
        port: int = 8788,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port

    async def run(self, stop: asyncio.Event) -> None:
        runner = web.AppRunner(create_dashboard_app(self.store), access_log=None)
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
