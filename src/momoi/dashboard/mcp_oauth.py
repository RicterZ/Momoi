"""Browser consent for MCP OAuth; background MCP workers never prompt."""

import asyncio
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx2
from aiohttp import web
from mcp.shared.auth import AuthorizationCodeResult

from ..mcp.config import expand_mcp_value, load_mcp_servers
from ..mcp.oauth import oauth_provider


async def obtain_tokens(provider, url, client):
    """Drive SDK discovery/PKCE without relying on a public MCP handshake's 401."""
    request = httpx2.Request("POST", url)
    flow = provider.async_auth_flow(request)
    try:
        await anext(flow)
        response = httpx2.Response(401, request=request)
        while True:
            outgoing = await flow.asend(response)
            if outgoing is request:
                if not await provider.context.storage.get_tokens():
                    raise ValueError("OAuth completed without tokens")
                break
            response = await client.send(outgoing)
            await response.aread()
    finally:
        await flow.aclose()


def register_mcp_oauth_routes(app, configuration, runtime):
    flows = {}

    async def start(request):
        name = request.match_info["name"]
        path = configuration.mcp_path()
        config = load_mcp_servers(path).get(name)
        if config is None or config.get("oauth") is None:
            raise web.HTTPBadRequest(text="Save an enabled OAuth MCP server first")
        existing = flows.get(name)
        if existing and not existing["task"].done():
            raise web.HTTPConflict(text="OAuth authorization is already pending")
        flow = {"status": "starting", "url": None, "state": None,
                "callback": asyncio.get_running_loop().create_future(), "ready": asyncio.Event()}
        flows[name] = flow

        async def redirect(url):
            parts = urlsplit(url)
            params = parse_qs(parts.query)
            flow["state"] = params["state"][0]
            # Google only issues a refresh token when offline access is requested.
            if parts.hostname == "accounts.google.com":
                params.update(access_type=["offline"], prompt=["consent"])
                url = urlunsplit(parts._replace(query=urlencode(params, doseq=True)))
            flow.update(status="pending", url=url)
            flow["ready"].set()

        async def callback():
            return await flow["callback"]

        async def authorize():
            try:
                async with asyncio.timeout(300):
                    provider = oauth_provider(path, name, config, redirect_handler=redirect, callback_handler=callback)
                    url = expand_mcp_value(config.get("url") or config.get("baseUrl") or config["serverUrl"])
                    # Do not recursively apply OAuth to discovery requests.
                    async with httpx2.AsyncClient(timeout=60) as discovery_client:
                        await obtain_tokens(provider, url, discovery_client)
                flow.update(status="starting", url=None)
                async with runtime.lock:
                    manager = getattr(runtime.daemon, "mcp", None)
                    if manager is not None:
                        await manager.reload()
                flow.update(status="complete", url=None)
            except asyncio.CancelledError:
                raise
            except Exception:
                # OAuth exceptions can contain codes, tokens or provider URLs.
                flow.update(status="error", url=None)
            finally:
                flow["state"] = None
                flow["ready"].set()

        flow["task"] = asyncio.create_task(authorize())
        try:
            async with asyncio.timeout(15):
                await flow["ready"].wait()
        except TimeoutError:
            pass
        return web.json_response({"status": flow["status"], "url": flow["url"]})

    async def status(request):
        flow = flows.get(request.match_info["name"])
        return web.json_response({"status": flow["status"] if flow else "idle",
                                  "url": flow["url"] if flow else None})

    async def callback(request):
        state = request.query.get("state", "")
        flow = next((item for item in flows.values() if item["state"] and
                     secrets.compare_digest(item["state"].encode(), state.encode())), None)
        if flow is None or flow["callback"].done():
            raise web.HTTPBadRequest(text="Invalid or expired OAuth callback")
        flow["state"] = None
        if request.query.get("error") or not request.query.get("code"):
            flow["callback"].set_exception(ValueError("OAuth consent was denied"))
            return web.Response(text="Authorization was not granted. Return to Momoi Dashboard.")
        flow["callback"].set_result(AuthorizationCodeResult(
            code=request.query["code"], state=state, iss=request.query.get("iss"),
        ))
        return web.Response(text="Authorization received. Return to Momoi Dashboard to check the result.")

    async def cleanup(_):
        tasks = [item["task"] for item in flows.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app.on_cleanup.append(cleanup)
    app.router.add_post("/api/tools/mcp/{name}/oauth", start)
    app.router.add_get("/api/tools/mcp/{name}/oauth", status)
    app.router.add_get("/api/tools/mcp/oauth/callback", callback)
