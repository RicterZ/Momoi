import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from momoi.channel import AmbiguousSend, SendRejected, create_channel
from momoi.channel.weixin import (
    WeixinChannel,
    WeixinConfig,
    WeixinState,
    login,
    render_segments,
)
from momoi.channel.weixin.media import (
    aes_key,
    decrypt,
    encrypt,
    read_source,
)


async def serve(handler):
    application = web.Application()
    application.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    socket = site._server.sockets[0]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{socket.getsockname()[1]}"


class WeixinTest(unittest.TestCase):
    def config(self, directory: str) -> WeixinConfig:
        return WeixinConfig.from_mapping({}, Path(directory))

    def test_config_state_and_aes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            self.assertEqual(config.quiet_seconds, 6)
            self.assertEqual(
                config.state_path,
                Path(directory).resolve() / "channel" / "weixin" / "state.json",
            )
            self.assertIsInstance(create_channel(config), WeixinChannel)
            with self.assertRaisesRegex(ValueError, "media_max_bytes"):
                WeixinConfig.from_mapping({"media_max_bytes": 0}, Path(directory))

            state = WeixinState("bot", "owner", "secret")
            state.save(config.state_path)
            self.assertEqual(os.stat(config.state_path).st_mode & 0o777, 0o600)
            self.assertEqual(WeixinState.load(config.state_path).token, "secret")  # type: ignore[union-attr]
            self.assertNotIn(
                "secret",
                config.state_path.with_suffix(".tmp").read_text()
                if config.state_path.with_suffix(".tmp").exists()
                else "",
            )

            key = bytes(range(16))
            plaintext = b"not aligned"
            self.assertEqual(decrypt(encrypt(plaintext, key), key), plaintext)
            self.assertEqual(aes_key(base64.b64encode(key).decode()), key)
            encoded_hex = base64.b64encode(key.hex().encode()).decode()
            self.assertEqual(aes_key(encoded_hex), key)

    def test_bounded_local_remote_and_base64_sources(self) -> None:
        async def run() -> None:
            async def handler(request: web.Request) -> web.Response:
                return web.Response(body=b"remote", content_type="image/png")

            runner, base = await serve(handler)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "local.txt"
                    path.write_bytes(b"local")
                    async with aiohttp.ClientSession() as session:
                        self.assertEqual(
                            (await read_source(session, str(path), 10))[0], b"local"
                        )
                        remote = await read_source(session, f"{base}/picture.png", 10)
                        self.assertEqual(
                            remote, (b"remote", "picture.png", "image/png")
                        )
                        encoded = "base64://" + base64.b64encode(b"inline").decode()
                        self.assertEqual(
                            (await read_source(session, encoded, 10))[0], b"inline"
                        )
                        with self.assertRaisesRegex(ValueError, "media_max_bytes"):
                            await read_source(session, encoded, 2)
            finally:
                await runner.cleanup()

        asyncio.run(run())

    def test_poll_filters_owner_maps_media_and_commits_cursor(self) -> None:
        async def run() -> None:
            key = b"0123456789abcdef"
            ciphertext = encrypt(b"\x89PNG\r\n\x1a\nimage", key)
            requests: list[tuple[str, dict[str, object], dict[str, str]]] = []
            base = ""

            async def handler(request: web.Request) -> web.Response:
                if request.path == "/image":
                    return web.Response(body=ciphertext)
                body = await request.json()
                requests.append((request.path, body, dict(request.headers)))
                if request.path.endswith("getupdates"):
                    return web.json_response(
                        {
                            "ret": 0,
                            "get_updates_buf": "next-cursor",
                            "msgs": [
                                {
                                    "message_id": 7,
                                    "from_user_id": "owner",
                                    "create_time_ms": 123000,
                                    "context_token": "context-7",
                                    "item_list": [
                                        {"type": 1, "text_item": {"text": "看看"}},
                                        {
                                            "type": 2,
                                            "image_item": {
                                                "media": {
                                                    "full_url": f"{base}/image",
                                                    "aes_key": base64.b64encode(
                                                        key
                                                    ).decode(),
                                                }
                                            },
                                        },
                                    ],
                                },
                                {
                                    "message_id": 8,
                                    "from_user_id": "intruder",
                                    "item_list": [
                                        {"type": 1, "text_item": {"text": "no"}}
                                    ],
                                },
                            ],
                        }
                    )
                return web.json_response({"ret": 0})

            runner, base = await serve(handler)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    config = self.config(directory)
                    WeixinState("bot", "owner", "token", base).save(config.state_path)
                    channel = WeixinChannel(config)
                    stop = asyncio.Event()
                    accepted = []

                    async def receive(message) -> None:
                        accepted.append(message)
                        stop.set()

                    await channel.run(receive, stop)
                    self.assertEqual(len(accepted), 1)
                    self.assertEqual(accepted[0].event_id, "weixin:bot:7")
                    self.assertIn("看看", accepted[0].text)
                    self.assertIn("Weixin image", accepted[0].text)
                    self.assertEqual(
                        len(channel.content_blocks(accepted[0].segments)), 1
                    )
                    saved = WeixinState.load(config.state_path)
                    self.assertEqual(saved.get_updates_buf, "next-cursor")  # type: ignore[union-attr]
                    self.assertEqual(saved.context_token, "context-7")  # type: ignore[union-attr]
                    poll = next(
                        item for item in requests if item[0].endswith("getupdates")
                    )
                    self.assertEqual(poll[1]["get_updates_buf"], "")
                    self.assertEqual(poll[1]["base_info"]["bot_agent"], "Momoi/0.1.0")  # type: ignore[index]
                    self.assertEqual(poll[2]["Authorization"], "Bearer token")
                    self.assertEqual(poll[2]["iLink-App-Id"], "bot")
            finally:
                await runner.cleanup()

        asyncio.run(run())

    def test_maps_quotes_video_files_and_untranscribed_voice(self) -> None:
        async def run() -> None:
            key = b"0123456789abcdef"
            encrypted = encrypt(b"attachment", key)

            async def handler(request: web.Request) -> web.Response:
                return web.Response(body=encrypted)

            runner, base = await serve(handler)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    channel = WeixinChannel(self.config(directory))
                    encoded_key = base64.b64encode(key).decode()

                    def media(path: str) -> dict[str, str]:
                        return {"full_url": f"{base}/{path}", "aes_key": encoded_key}

                    raw = {
                        "item_list": [
                            {
                                "type": 1,
                                "text_item": {"text": "current"},
                                "ref_msg": {
                                    "title": "quoted title",
                                    "message_item": {
                                        "type": 1,
                                        "text_item": {"text": "quoted body"},
                                    },
                                },
                            },
                            {"type": 3, "voice_item": {"media": media("voice")}},
                            {
                                "type": 4,
                                "file_item": {
                                    "media": media("file"),
                                    "file_name": "../notes.txt",
                                },
                            },
                            {"type": 5, "video_item": {"media": media("video")}},
                        ]
                    }
                    async with aiohttp.ClientSession() as session:
                        segments = await channel._segments(raw, session, "message")
                    self.assertEqual(
                        [item["type"] for item in segments],
                        ["reply", "text", "record", "file", "video"],
                    )
                    self.assertIn("quoted body", render_segments(segments))
                    self.assertEqual(segments[3]["data"]["name"], "notes.txt")
                    for item in segments[2:]:
                        self.assertTrue(Path(item["data"]["file"]).is_file())
            finally:
                await runner.cleanup()

        asyncio.run(run())

    def test_callback_failure_preserves_cursor_but_saves_context(self) -> None:
        async def run() -> None:
            async def handler(request: web.Request) -> web.Response:
                if request.path.endswith("getupdates"):
                    return web.json_response(
                        {
                            "ret": 0,
                            "get_updates_buf": "must-not-commit",
                            "msgs": [
                                {
                                    "message_id": "failed",
                                    "from_user_id": "owner",
                                    "context_token": "latest-context",
                                    "item_list": [
                                        {"type": 1, "text_item": {"text": "hello"}}
                                    ],
                                }
                            ],
                        }
                    )
                return web.json_response({"ret": 0})

            runner, base = await serve(handler)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    config = self.config(directory)
                    WeixinState("bot", "owner", "token", base, "old-cursor").save(
                        config.state_path
                    )
                    channel = WeixinChannel(config)

                    async def fail(_message) -> None:
                        raise LookupError("store unavailable")

                    with self.assertRaises(LookupError):
                        await channel.run(fail, asyncio.Event())
                    saved = WeixinState.load(config.state_path)
                    self.assertEqual(saved.get_updates_buf, "old-cursor")  # type: ignore[union-attr]
                    self.assertEqual(saved.context_token, "latest-context")  # type: ignore[union-attr]
            finally:
                await runner.cleanup()

        asyncio.run(run())

    def test_send_text_and_encrypted_image_in_segment_order(self) -> None:
        async def run() -> None:
            upload_request: dict[str, object] = {}
            upload_body = b""
            sent: list[dict[str, object]] = []
            base = ""

            async def handler(request: web.Request) -> web.Response:
                nonlocal upload_request, upload_body
                if request.path.endswith("getuploadurl"):
                    upload_request = await request.json()
                    return web.json_response({"upload_full_url": f"{base}/upload"})
                if request.path == "/upload":
                    upload_body = await request.read()
                    return web.Response(headers={"x-encrypted-param": "download-param"})
                if request.path.endswith("sendmessage"):
                    sent.append(await request.json())
                    return web.json_response({"ret": 0})
                return web.json_response({"ret": 0})

            runner, base = await serve(handler)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    config = self.config(directory)
                    WeixinState(
                        "bot", "owner", "token", base, context_token="context"
                    ).save(config.state_path)
                    channel = WeixinChannel(config)
                    async with aiohttp.ClientSession() as session:
                        channel._session = session
                        channel._ready.set()
                        source = "base64://" + base64.b64encode(b"image bytes").decode()
                        identifier = await channel.send_message(
                            {
                                "action": "message",
                                "segments": [
                                    {"type": "text", "data": {"text": "caption"}},
                                    {"type": "image", "data": {"file": source}},
                                ],
                            }
                        )
                    self.assertRegex(
                        identifier, r"^openclaw-weixin:\d{13}-[0-9a-f]{8}$"
                    )
                    self.assertEqual(
                        [entry["msg"]["item_list"][0]["type"] for entry in sent], [1, 2]
                    )  # type: ignore[index]
                    key = bytes.fromhex(str(upload_request["aeskey"]))
                    self.assertEqual(decrypt(upload_body, key), b"image bytes")
                    image_media = sent[1]["msg"]["item_list"][0]["image_item"]["media"]  # type: ignore[index]
                    self.assertEqual(base64.b64decode(image_media["aes_key"]), key)
            finally:
                await runner.cleanup()

        asyncio.run(run())

    def test_send_rejection_and_partial_send_are_distinct(self) -> None:
        async def run() -> None:
            calls = 0

            async def handler(request: web.Request) -> web.Response:
                nonlocal calls
                if request.path.endswith("sendmessage"):
                    calls += 1
                    return web.json_response({"ret": 0 if calls == 1 else 12})
                return web.json_response({"ret": 0})

            runner, base = await serve(handler)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    config = self.config(directory)
                    WeixinState(
                        "bot", "owner", "token", base, context_token="context"
                    ).save(config.state_path)
                    channel = WeixinChannel(config)
                    async with aiohttp.ClientSession() as session:
                        channel._session = session
                        channel._ready.set()
                        with self.assertRaises(SendRejected):
                            await channel.send_message(
                                {
                                    "action": "message",
                                    "segments": [
                                        {"type": "reply", "data": {"id": "1"}}
                                    ],
                                }
                            )
                        with self.assertRaises(AmbiguousSend):
                            await channel.send_message(
                                {
                                    "action": "message",
                                    "segments": [
                                        {"type": "text", "data": {"text": "one"}},
                                        {"type": "text", "data": {"text": "two"}},
                                    ],
                                }
                            )
            finally:
                await runner.cleanup()

        asyncio.run(run())

    def test_qr_login_and_already_bound_state(self) -> None:
        async def run() -> None:
            mode = "confirmed"
            qr_bodies: list[dict[str, object]] = []

            async def handler(request: web.Request) -> web.Response:
                if request.path.endswith("get_bot_qrcode"):
                    qr_bodies.append(await request.json())
                    return web.json_response(
                        {
                            "qrcode": "private-qr-token",
                            "qrcode_img_content": "https://qr.test/1",
                        }
                    )
                if request.path.endswith("get_qrcode_status"):
                    if mode == "confirmed":
                        return web.json_response(
                            {
                                "status": "confirmed",
                                "bot_token": "new-token",
                                "ilink_bot_id": "new-bot",
                                "ilink_user_id": "owner",
                                "baseurl": base,
                            }
                        )
                    return web.json_response({"status": "binded_redirect"})
                return web.json_response({})

            runner, base = await serve(handler)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    config = self.config(directory)
                    with (
                        patch("momoi.channel.weixin.api.DEFAULT_BASE_URL", base),
                        patch("qrcode.QRCode.print_ascii"),
                        patch("builtins.print"),
                    ):
                        await login(config)
                    state = WeixinState.load(config.state_path)
                    self.assertEqual(state.account_id, "new-bot")  # type: ignore[union-attr]
                    self.assertEqual(state.get_updates_buf, "")  # type: ignore[union-attr]
                    self.assertEqual(qr_bodies[0], {"local_token_list": []})

                    mode = "bound"
                    before = config.state_path.read_text()
                    with (
                        patch("momoi.channel.weixin.api.DEFAULT_BASE_URL", base),
                        patch("qrcode.QRCode.print_ascii"),
                        patch("builtins.print"),
                    ):
                        await login(config)
                    self.assertEqual(config.state_path.read_text(), before)
                    self.assertEqual(qr_bodies[-1], {"local_token_list": ["new-token"]})
            finally:
                await runner.cleanup()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
