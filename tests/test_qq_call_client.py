"""Real HTTP bridge probing works without local Linux audio tools."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.channel.napcat.config import QQCallConfig
from momoi.qq_call.client import probe


def test_remote_bridge_probe_authentication_and_readiness():
    async def scenario():
        ready = True
        async def status(request):
            if request.headers.get("Authorization") != "Bearer " + "a" * 64:
                raise web.HTTPUnauthorized()
            return web.json_response({"protocol_version": 1, "ready": ready,
                                      "dependencies": {"bridge": True, "av_host": True, "audio": ready},
                                      "error": "" if ready else "audio unavailable"})
        app = web.Application()
        app.router.add_get("/v1/status", status)
        async with TestServer(app) as server:
            config = QQCallConfig.from_mapping({"enabled": True, "bridge_url": str(server.make_url("")).rstrip("/"), "bridge_token": "a" * 64})
            result = await probe(config)
            assert result["ok"] and result["dependencies"]["audio"]
            ready = False
            result = await probe(config)
            assert not result["ok"] and result["error"] == "audio unavailable"
            config = QQCallConfig.from_mapping({"enabled": True, "bridge_url": config.bridge_url, "bridge_token": "b" * 64})
            result = await probe(config)
            assert not result["ok"] and result["error"] == "Bridge 认证失败"
    asyncio.run(scenario())
