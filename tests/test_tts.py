import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.config.loading import load_config
from momoi.config.models import ConfigError
from momoi.tts import TTSError, create_tts_provider
from momoi.tts.fish import FishAudioTTSProvider


class FishTTSTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.requests = []
        self.status = 200
        self.content_type = "audio/mpeg"
        self.audio = b"ID3-test-audio"
        self.delay = 0
        self.stream = False

        async def handle(request):
            self.requests.append((dict(request.headers), await request.json()))
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.stream:
                response = web.StreamResponse(headers={"Content-Type": self.content_type})
                await response.prepare(request)
                await response.write(self.audio)
                await response.write_eof()
                return response
            return web.Response(status=self.status, body=self.audio, content_type=self.content_type)

        app = web.Application()
        app.router.add_post("/v1/tts", handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)

    def provider(self, **kwargs):
        return FishAudioTTSProvider(**{
            "api_key": "test-key", "reference_id": "momoi-voice",
            "base_url": str(self.server.make_url("")),
            **kwargs,
        })

    async def test_request_returns_audio_in_memory(self):
        text = "你好，老师。\n\n今天一起玩游戏吧！"
        audio = await self.provider(latency="balanced").synthesize(text)
        self.assertEqual(audio.data, self.audio)
        self.assertEqual(audio.format, "mp3")
        self.assertEqual(list(self.root.iterdir()), [])
        headers, body = self.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["model"], "s2.1-pro-free")
        self.assertEqual(body, {
            "text": text, "reference_id": "momoi-voice", "format": "mp3", "latency": "balanced",
        })
        second = await self.provider().synthesize(text)
        self.assertEqual(second.data, self.audio)

    async def test_http_errors_do_not_retry_or_leak_response(self):
        for status in (302, 401, 429, 500):
            with self.subTest(status=status):
                self.status = status
                self.audio = b"test-key private spoken text"
                with self.assertRaises(TTSError) as caught:
                    await self.provider().synthesize("private spoken text")
                self.assertEqual(str(caught.exception), f"Fish TTS returned HTTP {status}")
        self.assertEqual(len(self.requests), 4)
        self.assertFalse((self.root / "audio").exists())

    async def test_bad_or_oversized_audio_is_rejected(self):
        for content_type, audio, limit, stream in (
            ("application/json", b'{"error":"failed"}', 100, False),
            ("audio/mpeg", b"", 100, False),
            ("audio/mpeg", b"123456", 5, False),
            ("audio/mpeg", b"123456", 5, True),
        ):
            with self.subTest(content_type=content_type, audio=audio, stream=stream):
                self.content_type, self.audio, self.stream = content_type, audio, stream
                with self.assertRaises(TTSError):
                    await self.provider(max_audio_bytes=limit).synthesize("hello")
                self.assertEqual(list((self.root / "audio").glob("*")), [])

    async def test_timeout_and_blank_text(self):
        with self.assertRaises(TTSError):
            await self.provider().synthesize(" \n ")
        self.assertEqual(self.requests, [])
        self.delay = 0.1
        with self.assertRaisesRegex(TTSError, "TimeoutError"):
            await self.provider(timeout_seconds=0.01).synthesize("hello")
        self.assertEqual(list((self.root / "audio").glob("*")), [])

    def write_config(self, tts_config):
        (self.root / "prompts").mkdir(exist_ok=True)
        (self.root / "prompts" / "SOUL.md").write_text("Test soul")
        raw = {
            "llm": {"base_url": "http://localhost", "api_key": "test", "model": "test"},
            "channels": {"primary": "napcat", "enabled": {"napcat": {"url": "ws://localhost", "owner_qq": "123"}}},
            "context": {}, "storage": {"database": "data/momoi.sqlite3"}, "logging": {},
            "tts": tts_config, "tools": {"mcp_config": None},
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(raw))
        return path

    async def test_config_factory_and_runtime_injection(self):
        path = self.write_config({})
        self.assertIsNone(create_tts_provider(load_config(path)))
        settings = {"api_key": "config-key", "reference_id": "momoi-voice", "base_url": str(self.server.make_url(""))}
        path = self.write_config({"enabled": True, "settings": settings})
        with patch.dict("os.environ", {"MOMOI_TTS_API_KEY": "env-key"}):
            config = load_config(path)
            provider = create_tts_provider(config)
            self.assertEqual(provider.api_key, "config-key")
            from momoi.runtime import MomoiDaemon
            daemon = MomoiDaemon(config)
            try:
                self.assertIsInstance(daemon.bubble_delivery.tts_provider, FishAudioTTSProvider)
                self.assertIn("send_voice", {s["name"] for s in daemon.tool_surface.conversation_specs()})
            finally:
                daemon.store.close()
            audio = await provider.synthesize("provider 测试")
        self.assertEqual(audio.data, self.audio)
        self.assertEqual(self.requests[0][0]["Authorization"], "Bearer config-key")

    def test_invalid_configuration_is_rejected_before_requests(self):
        baseline = {"enabled": True, "settings": {"api_key": "test", "reference_id": "voice"}}
        cases = [
            {"provider": "unknown"}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
            {"max_audio_bytes": True}, {"output_dir": ""}, {"enabled": "true"},
        ]
        for field, value in (("model", "s2.1-pro-fre"), ("format", "pcm"), ("latency", "fast"),
                             ("base_url", "bad-url"), ("api_key", ""), ("reference_id", 123)):
            cases.append({"settings": {**baseline["settings"], field: value}})
        with patch.dict("os.environ", {"MOMOI_TTS_API_KEY": ""}):
            for fields in cases:
                with self.subTest(fields=fields):
                    with self.assertRaises(ConfigError):
                        load_config(self.write_config({**baseline, **fields}))
        self.assertEqual(self.requests, [])
