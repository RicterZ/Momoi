"""HTTP presentation for configuration management and runtime status."""

import asyncio
import base64
import io

import qrcode
import qrcode.image.svg
from aiohttp import web

from ..channel.weixin.api import login
from ..channel.weixin.config import WeixinConfig
from ..config.manager import RevisionConflict
from ..config.models import ConfigError
from ..integrations.testing import failure, test_connection


class ChannelLogin:
    def __init__(self, configuration, runtime):
        self.configuration = configuration
        self.runtime = runtime
        self.task = None
        self.state = {"status": "idle"}
        self.codes = asyncio.Queue(maxsize=1)
        self.lock = asyncio.Lock()

    async def update(self, status, **data):
        self.state = {"status": status}
        if content := data.get("qr_content"):
            output = io.BytesIO()
            qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage).save(
                output
            )
            self.state["qr_image"] = (
                "data:image/svg+xml;base64,"
                + base64.b64encode(output.getvalue()).decode()
            )

    async def start(self):
        async with self.lock:
            return await self._start()

    async def _start(self):
        if self.task is not None and not self.task.done():
            return self.state
        config = self.configuration.validate()
        selected = next(
            (item for item in config.channel_configs if isinstance(item, WeixinConfig)),
            None,
        )
        if selected is None:
            raise ConfigError("enable the Weixin channel before logging in")
        await self.runtime.pause()
        self.codes = asyncio.Queue(maxsize=1)
        self.state = {"status": "starting"}

        async def authenticate():
            try:
                async with asyncio.timeout(480):
                    await login(
                        selected, on_update=self.update, verification=self.codes.get
                    )
            except asyncio.CancelledError:
                self.state = {"status": "cancelled"}
                raise
            except Exception as error:
                self.state = {"status": "error", "error": type(error).__name__}
            finally:
                self.runtime.resume()

        self.task = asyncio.create_task(authenticate())
        return self.state

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def register_configuration_routes(app, configuration, runtime):
    channel_login = ChannelLogin(configuration, runtime)
    test_lock = asyncio.Lock()

    async def test_provider(request):
        capability = request.match_info["capability"]
        try:
            value = await request.json()
        except ValueError:
            value = None
        if not isinstance(value, dict):
            return web.json_response(
                failure(capability, None, "validation", "请求体必须是 JSON 对象。"),
                status=400,
            )
        if test_lock.locked():
            return web.json_response(
                failure(capability, value.get("adapter"), "busy", "已有连接测试正在进行，请稍后重试。"),
                status=429,
            )
        async with test_lock:
            status_code, result = await test_connection(capability, value)
        return web.json_response(result, status=status_code)

    async def body(request):
        try:
            value = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return value

    async def snapshot(request):
        return web.json_response(configuration.snapshot())

    async def mcp_config(request):
        if request.method == "PATCH":
            value = await body(request)
            try:
                revision = request.headers.get("If-Match")
                result = configuration.save_mcp(value, revision.strip('"') if revision else None)
            except RevisionConflict as error:
                raise web.HTTPConflict(text=str(error)) from None
            except (ValueError, TypeError, KeyError, OSError) as error:
                raise web.HTTPBadRequest(text=f"invalid MCP configuration: {type(error).__name__}") from None
            runtime.request_apply()
            return web.json_response(result, status=202)
        reference = configuration.read_app().get("tools", {}).get("mcp_config", "mcp.json")
        path = configuration.mcp_path()
        headers = {"ETag": f'"{configuration.revision()}"'}
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            if reference and reference != "mcp.json":
                raise web.HTTPNotFound(text="MCP configuration file not found") from None
            return web.json_response({"mcpServers": {}}, headers=headers)
        except OSError:
            raise web.HTTPInternalServerError(text="Cannot read MCP configuration") from None
        return web.Response(body=content, content_type="application/json", headers=headers)

    async def save(request):
        value = await body(request)
        try:
            if request.path == "/api/settings/providers":
                result = configuration.save_bindings(
                    value.get("document"), value.get("revision")
                )
            elif request.method == "PATCH":
                result = configuration.save_runtime(
                    value.get("document"), value.get("revision")
                )
            elif "capability" in request.match_info:
                result = configuration.save_binding(
                    request.match_info["capability"],
                    value.get("document"),
                    value.get("revision"),
                )
            else:
                result = configuration.save(
                    request.match_info["section"],
                    value.get("document"),
                    value.get("revision"),
                )
        except RevisionConflict as error:
            raise web.HTTPConflict(text=str(error)) from None
        except (ValueError, TypeError, KeyError, OSError) as error:
            message = (
                str(error)
                if isinstance(error, ConfigError)
                else f"invalid configuration: {type(error).__name__}"
            )
            raise web.HTTPBadRequest(text=message) from None
        runtime.request_apply()
        return web.json_response(result)

    async def status(request):
        return web.json_response(
            {**runtime.status(), "weixin_login": channel_login.state}
        )

    async def apply(request):
        runtime.request_apply()
        return web.json_response(runtime.status(), status=202)

    async def start_login(request):
        try:
            result = await channel_login.start()
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return web.json_response(result, status=202)

    async def verify(request):
        value = await body(request)
        code = value.get("code")
        if (
            not isinstance(code, str)
            or not code.isascii()
            or not code.isdigit()
            or len(code) > 12
        ):
            raise web.HTTPBadRequest(text="verification code must contain digits")
        if (
            channel_login.state["status"] != "verification_required"
            or channel_login.codes.full()
        ):
            raise web.HTTPConflict(text="no verification code is expected")
        channel_login.codes.put_nowait(code)
        return web.json_response({"ok": True})

    async def cancel_login(request):
        await channel_login.close()
        return web.json_response(channel_login.state)

    async def cleanup(app):
        await channel_login.close()

    app.on_cleanup.append(cleanup)
    app.router.add_get("/api/settings/configuration", snapshot)
    app.router.add_post("/api/settings/providers/{capability}/test", test_provider)
    app.router.add_get("/api/settings/mcp", mcp_config)
    app.router.add_patch("/api/settings/mcp", mcp_config)
    app.router.add_put("/api/settings/configuration/{section}", save)
    app.router.add_put("/api/settings/providers/{capability}", save)
    app.router.add_put("/api/settings/providers", save)
    app.router.add_patch("/api/settings/configuration/app", save)
    app.router.add_get("/api/settings/runtime", status)
    app.router.add_post("/api/settings/apply", apply)
    app.router.add_post("/api/settings/channels/weixin/login", start_login)
    app.router.add_post("/api/settings/channels/weixin/verify", verify)
    app.router.add_delete("/api/settings/channels/weixin/login", cancel_login)
