import asyncio
import copy
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import httpx2
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from mcp.shared.auth import AuthorizationCodeResult, OAuthMetadata, OAuthToken

from momoi.dashboard.auth import DASHBOARD_TOKEN, _auth, issue_dashboard_jwt
from momoi.dashboard.mcp_oauth import obtain_tokens, register_mcp_oauth_routes
from momoi.mcp.config import parse_mcp_servers
from momoi.mcp.oauth import MCPOAuthProvider, oauth_provider, validate_oauth
from momoi.mcp.manager import MCPManager


SERVER = {
    "description": "Read mail", "serverUrl": "https://resource.example/mcp",
    "oauth": {"clientId": "client", "clientSecret": "secret",
              "redirectUri": "https://dashboard.example/api/tools/mcp/oauth/callback", "scope": "mail.read"},
}
METADATA = {
    "issuer": "https://auth.example", "authorization_endpoint": "https://auth.example/authorize",
    "token_endpoint": "https://auth.example/token", "response_types_supported": ["code"],
    "token_endpoint_auth_methods_supported": ["client_secret_post"],
    "code_challenge_methods_supported": ["S256"],
}


class OAuthTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "mcp.json"

    def test_config_alias_and_validation(self):
        provider = oauth_provider(self.path, "mail", SERVER)
        self.assertEqual(provider._select_authorization_server(["https://accounts.google.com/"]), "https://accounts.google.com")
        self.assertEqual(provider._select_authorization_server(["https://other.example/path/"]), "https://other.example/path/")
        self.assertEqual(parse_mcp_servers(json.dumps({"mcpServers": {"mail": SERVER}}))["mail"], SERVER)
        for change in ({"clientId": ""}, {"redirectUri": "http://remote.example/api/tools/mcp/oauth/callback"},
                       {"redirectUri": "https://dashboard.example/other"}, {"clientSecret": 1},
                       {"redirectUri": "https://dashboard.example/api/tools/mcp/oauth/callback?token=secret"}):
            config = copy.deepcopy(SERVER)
            config["oauth"].update(change)
            with self.assertRaises(ValueError):
                validate_oauth(config)
        for changes in ({"command": "test"}, {"headers": {"AUTHORIZATION": "Bearer secret"}}, {"oauth": []}):
            with self.assertRaises(ValueError):
                validate_oauth({**SERVER, **changes})

    async def test_private_storage_expiry_refresh_and_identity(self):
        provider = oauth_provider(self.path, "mail", SERVER)
        provider.context.oauth_metadata = OAuthMetadata.model_validate(METADATA)
        provider.context.auth_server_url = "https://auth.example"
        with patch("momoi.mcp.oauth.time.time", return_value=100):
            await provider.context.storage.set_tokens(OAuthToken(access_token="old", refresh_token="refresh", expires_in=20))
        with patch("momoi.mcp.oauth.time.time", return_value=125):
            restored = oauth_provider(self.path, "mail", SERVER)
            tokens = await restored.context.storage.get_tokens()
        self.assertEqual(tokens.expires_in, 0)
        self.assertEqual(restored.context.token_expiry_time, 120)
        self.assertEqual(str(restored.context.oauth_metadata.token_endpoint), "https://auth.example/token")
        if os.name != "nt":
            self.assertEqual(provider.context.storage.path.stat().st_mode & 0o777, 0o600)

        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/token":
                data = parse_qs(request.content.decode())
                self.assertEqual(data["grant_type"], ["refresh_token"])
                self.assertEqual(data["refresh_token"], ["refresh"])
                self.assertEqual(data["client_secret"], ["secret"])
                return httpx2.Response(200, json={"access_token": "new", "token_type": "Bearer", "expires_in": 3600})
            self.assertEqual(request.headers["Authorization"], "Bearer new")
            return httpx2.Response(200, json={"ok": True})

        async with httpx2.AsyncClient(auth=restored, transport=httpx2.MockTransport(respond)) as client:
            response = await client.get(SERVER["serverUrl"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 2)
        self.assertEqual((await restored.context.storage.get_tokens()).refresh_token, "refresh")
        changed = copy.deepcopy(SERVER)
        changed["oauth"]["clientId"] = "another-client"
        self.assertIsNone(await oauth_provider(self.path, "mail", changed).context.storage.get_tokens())

    async def test_sdk_consent_pkce_exchange_and_noninteractive_failure(self):
        callback_result = None

        async def redirect(url):
            nonlocal callback_result
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params["code_challenge_method"], ["S256"])
            self.assertEqual(params["client_id"], ["client"])
            callback_result = AuthorizationCodeResult(code="code", state=params["state"][0])

        async def callback():
            return callback_result

        def respond(request):
            if request.url.host == "resource.example" and request.url.path == "/mcp":
                if request.headers.get("Authorization") == "Bearer access":
                    return httpx2.Response(200, json={"ok": True})
                return httpx2.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://resource.example/.well-known/oauth-protected-resource"'})
            if request.url.host == "resource.example":
                return httpx2.Response(200, json={"resource": SERVER["serverUrl"], "authorization_servers": ["https://auth.example"], "scopes_supported": ["mail.read"]})
            if request.url.path == "/token":
                data = parse_qs(request.content.decode())
                self.assertEqual(data["code"], ["code"])
                self.assertTrue(data["code_verifier"][0])
                return httpx2.Response(200, json={"access_token": "access", "refresh_token": "refresh", "token_type": "Bearer", "expires_in": 3600})
            return httpx2.Response(200, json=METADATA)

        provider = oauth_provider(self.path, "mail", SERVER, redirect_handler=redirect, callback_handler=callback)
        async with httpx2.AsyncClient(auth=provider, transport=httpx2.MockTransport(respond)) as client:
            self.assertEqual((await client.get(SERVER["serverUrl"])).status_code, 200)
        consent = oauth_provider(self.path, "public-handshake", SERVER, redirect_handler=redirect, callback_handler=callback)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
            await obtain_tokens(consent, SERVER["serverUrl"], client)
        self.assertIsNotNone(await consent.context.storage.get_tokens())
        unattended = oauth_provider(self.path, "unauthorized", SERVER)
        with self.assertLogs("mcp.client.auth.oauth2", level=logging.ERROR) as logs:
            async with httpx2.AsyncClient(auth=unattended, transport=httpx2.MockTransport(respond)) as client:
                with self.assertRaisesRegex(ValueError, "authenticate this server in Dashboard"):
                    await client.get(SERVER["serverUrl"])
        self.assertNotIn("secret", "".join(logs.output))

    async def test_real_sdk_remote_transport_discovery_and_call(self):
        async def handle(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer access")
            message = await request.json()
            if "id" not in message:
                return web.Response(status=202)
            method = message["method"]
            if method == "initialize":
                result = {"protocolVersion": message["params"]["protocolVersion"],
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "test", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "read", "description": "Read mail", "inputSchema": {"type": "object"},
                                     "annotations": {"readOnlyHint": True}}]}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "mail content"}], "isError": False}
            else:
                raise AssertionError(method)
            return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})

        app = web.Application()
        app.router.add_post("/mcp", handle)
        async with TestServer(app) as server:
            config = {**SERVER, "serverUrl": str(server.make_url("/mcp"))}
            provider = oauth_provider(self.path, "mail", config)
            await provider.context.storage.set_tokens(OAuthToken(access_token="access", expires_in=3600))
            async with MCPManager(self.path, servers={"mail": config}) as manager:
                self.assertFalse(manager.errors)
                self.assertEqual(manager.tool_specs[0]["input_schema"], {"type": "object"})
                self.assertEqual(manager.capability("mcp__mail__read"), "read")
                result = await manager._invoke("mail", "read", {})
                self.assertTrue(result["ok"])
                self.assertEqual(result["result"]["content"][0]["text"], "mail content")


class DashboardOAuthTest(unittest.IsolatedAsyncioTestCase):
    async def test_callback_state_authentication_completion_and_replay(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"mail": SERVER}}))
        runtime = SimpleNamespace(lock=asyncio.Lock(), daemon=SimpleNamespace(mcp=SimpleNamespace(reload=AsyncMock())))
        provider = None

        def make_provider(*args, **kwargs):
            nonlocal provider
            provider = SimpleNamespace(**kwargs)
            return None

        async def obtain(*args):
            await provider.redirect_handler("https://accounts.google.com/o/oauth2/auth?state=test-state")
            self_result.append(await provider.callback_handler())

        self_result = []
        app = web.Application(middlewares=[_auth])
        app[DASHBOARD_TOKEN] = "dashboard-secret"
        register_mcp_oauth_routes(app, SimpleNamespace(mcp_path=lambda: path), runtime)
        async with TestClient(TestServer(app)) as client:
            headers = {"Authorization": "Bearer " + issue_dashboard_jwt("dashboard-secret")}
            endpoint = "/api/tools/mcp/mail/oauth"
            self.assertEqual((await client.post(endpoint)).status, 401)
            self.assertEqual((await client.get(endpoint)).status, 401)
            self.assertEqual((await client.get("/api/tools/mcp/oauth/callback?state=wrong&code=code")).status, 400)
            with patch("momoi.dashboard.mcp_oauth.oauth_provider", make_provider), patch("momoi.dashboard.mcp_oauth.obtain_tokens", obtain):
                response = await client.post(endpoint, headers=headers)
                flow = await response.json()
                self.assertEqual(flow["status"], "pending")
                self.assertIn("access_type=offline", flow["url"])
                self.assertEqual((await client.post(endpoint, headers=headers)).status, 409)
                self.assertEqual((await client.get("/api/tools/mcp/oauth/callback?state=test-state&code=code&iss=https://auth.example")).status, 200)
                await asyncio.sleep(0)
                self.assertEqual((await client.get("/api/tools/mcp/oauth/callback?state=test-state&code=code")).status, 400)
                status = await (await client.get(endpoint, headers=headers)).json()
                self.assertEqual(status["status"], "complete")
                self.assertIsNone(status["url"])
                self.assertEqual(self_result[0].iss, "https://auth.example")
                runtime.daemon.mcp.reload.assert_awaited_once()
