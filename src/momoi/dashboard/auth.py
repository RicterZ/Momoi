import base64
import hashlib
import hmac
import json
import re
import time

from aiohttp import web

DASHBOARD_TOKEN = web.AppKey("dashboard_token", str)
JWT_TTL_SECONDS = 365 * 24 * 60 * 60
JWT_SUBJECT = "momoi-dashboard"
_PUBLIC_ASSET_PATH = re.compile(r"^/api/emotions/[^/]+/asset$")
_AUTH_TOKEN_PATH = "/api/auth/token"


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


def verify_dashboard_jwt(token: str, secret: str, *, now: int | None = None) -> bool:
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


@web.middleware
async def _auth(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    if request.path.startswith("/api/") and not _is_public_api(request):
        _require_token(request)
    return await handler(request)
