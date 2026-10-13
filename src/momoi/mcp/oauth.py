"""SDK OAuth integration with workspace-local, private token storage."""

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken, ProtectedResourceMetadata

from .config import expand_mcp_value


class _SafeOAuthErrors(logging.Filter):
    def filter(self, record):
        if record.exc_info:
            record.msg = "MCP OAuth failed (%s)"
            record.args = (record.exc_info[0].__name__,)
            record.exc_info = None
            record.exc_text = None
        return True


# The SDK's token-error exceptions include the provider's raw response body.
logging.getLogger("mcp.client.auth.oauth2").addFilter(_SafeOAuthErrors())


class MCPOAuthProvider(OAuthClientProvider):
    def _select_authorization_server(self, advertised):
        issuer = super()._select_authorization_server(advertised)
        # Google PRM advertises a slash that its RFC 8414 issuer omits.
        return "https://accounts.google.com" if issuer == "https://accounts.google.com/" else issuer


def validate_oauth(config):
    oauth = config.get("oauth")
    if oauth is None:
        return
    if not isinstance(oauth, dict):
        raise ValueError("oauth must be an object")
    if config.get("command") or not any(config.get(key) for key in ("url", "baseUrl", "serverUrl")):
        raise ValueError("OAuth requires a remote HTTP server")
    for key in ("clientId", "redirectUri"):
        if not isinstance(oauth.get(key), str) or not oauth[key].strip():
            raise ValueError(f"oauth.{key} is required")
    for key in ("clientSecret", "scope"):
        if key in oauth and not isinstance(oauth[key], str):
            raise ValueError(f"oauth.{key} must be a string")
    uri = urlsplit(expand_mcp_value(oauth["redirectUri"]))
    if (uri.scheme not in {"http", "https"} or not uri.hostname or uri.username
            or uri.password or uri.query or uri.fragment
            or uri.path != "/api/tools/mcp/oauth/callback"):
        raise ValueError("oauth.redirectUri must be the Dashboard /api/tools/mcp/oauth/callback URL")
    if uri.scheme == "http" and uri.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("OAuth redirect URI requires HTTPS except on localhost")
    if any(str(key).lower() == "authorization" for key in config.get("headers", {})):
        raise ValueError("OAuth cannot be combined with an Authorization header")


class OAuthStorage:
    def __init__(self, path: Path, client_info: OAuthClientInformationFull):
        self.path = path
        self.client_info = client_info
        self.context = None

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info

    async def get_tokens(self):
        data = self.read()
        if not data:
            return None
        tokens = OAuthToken.model_validate(data["tokens"])
        if data.get("expires_at") is not None:
            tokens.expires_in = max(0, int(data["expires_at"] - time.time()))
        return tokens

    async def set_tokens(self, tokens):
        # Some providers omit refresh_token when refreshing; retain the existing one.
        previous = await self.get_tokens()
        if previous and not tokens.refresh_token:
            tokens = tokens.model_copy(update={"refresh_token": previous.refresh_token})
        data = {"tokens": tokens.model_dump(mode="json"), "expires_at":
                time.time() + tokens.expires_in if tokens.expires_in is not None else None}
        if self.context:
            data.update(
                oauth_metadata=self.context.oauth_metadata.model_dump(mode="json") if self.context.oauth_metadata else None,
                protected_resource_metadata=self.context.protected_resource_metadata.model_dump(mode="json") if self.context.protected_resource_metadata else None,
                auth_server_url=self.context.auth_server_url,
            )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def oauth_provider(path, name, config, *, redirect_handler=None, callback_handler=None):
    validate_oauth(config)
    if path is None:
        raise ValueError("OAuth requires a workspace MCP configuration path")
    oauth = {key: expand_mcp_value(value) for key, value in config["oauth"].items()}
    url = expand_mcp_value(config.get("url") or config.get("baseUrl") or config["serverUrl"])
    metadata = OAuthClientMetadata(
        redirect_uris=[oauth["redirectUri"]], client_name="Momoi", application_type="web",
        scope=oauth.get("scope") or None,
        token_endpoint_auth_method="client_secret_post" if oauth.get("clientSecret") else "none",
    )
    client = OAuthClientInformationFull(
        **metadata.model_dump(), client_id=oauth["clientId"], client_secret=oauth.get("clientSecret") or None,
    )
    identity = json.dumps([name, url, oauth], sort_keys=True).encode()
    filename = hashlib.sha256(identity).hexdigest() + ".json"
    storage = OAuthStorage(Path(path).parent / "var" / "mcp-oauth" / filename, client)

    async def authentication_required(_=None):
        raise ValueError("OAuth authorization required; authenticate this server in Dashboard")

    provider = MCPOAuthProvider(
        server_url=url, client_metadata=metadata, storage=storage,
        redirect_handler=redirect_handler or authentication_required,
        callback_handler=callback_handler or authentication_required,
    )
    storage.context = provider.context
    saved = storage.read()
    # SDK initialization loads tokens, but not their absolute expiry or discovered
    # endpoints. Restore these so a restarted daemon can refresh without consent.
    if saved:
        provider.context.token_expiry_time = saved.get("expires_at")
        if saved.get("oauth_metadata"):
            provider.context.oauth_metadata = OAuthMetadata.model_validate(saved["oauth_metadata"])
        if saved.get("protected_resource_metadata"):
            provider.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(saved["protected_resource_metadata"])
        provider.context.auth_server_url = saved.get("auth_server_url")
    return provider
