import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.config.loading import load_config
from momoi.integrations.registry import ServiceRegistry
import yaml
from momoi.config.models import ConfigError
from momoi.integrations.contracts.tts import TTSError
from momoi.integrations.adapters.fish import FishAudioTTSProvider


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
        retries = patch("momoi.integrations.adapters.fish.TTS_RETRY_DELAYS", (0, 0, 0))
        retries.start()
        self.addCleanup(retries.stop)

        async def handle(request):
            self.requests.append((dict(request.headers), await request.json()))
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.stream:
                response = web.StreamResponse(
                    headers={"Content-Type": self.content_type}
                )
                await response.prepare(request)
                await response.write(self.audio)
                await response.write_eof()
                return response
            return web.Response(
                status=self.status, body=self.audio, content_type=self.content_type
            )

        app = web.Application()
        app.router.add_post("/v1/tts", handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)

    def provider(self, **kwargs):
        return FishAudioTTSProvider(
            **{
                "api_key": "test-key",
                "reference_id": "momoi-voice",
                "base_url": str(self.server.make_url("")),
                **kwargs,
            }
        )

    async def test_request_returns_audio_in_memory(self):
        text = "你好，老师。\n\n今天一起玩游戏吧！"
        audio = await self.provider(latency="balanced").synthesize(text)
        self.assertEqual(audio.data, self.audio)
        self.assertEqual(audio.format, "mp3")
        self.assertEqual(list(self.root.iterdir()), [])
        headers, body = self.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["model"], "s2.1-pro-free")
        self.assertEqual(
            body,
            {
                "text": text,
                "reference_id": "momoi-voice",
                "format": "mp3",
                "latency": "balanced",
            },
        )
        second = await self.provider().synthesize(text)
        self.assertEqual(second.data, self.audio)

    async def test_http_errors_retry_three_times_and_redact_sensitive_details(self):
        for status in (302, 401, 429, 500):
            with self.subTest(status=status):
                self.status = status
                self.audio = b'{"error":"backend unavailable", "key":"test-key", "text":"private spoken text"}'
                with self.assertLogs(
                    "momoi.integrations.adapters.fish", level="WARNING"
                ) as logs:
                    with self.assertRaises(TTSError) as caught:
                        await self.provider().synthesize("private spoken text")
                self.assertIn(f"Fish TTS returned HTTP {status}", str(caught.exception))
                self.assertIn("backend unavailable", str(caught.exception))
                self.assertIn("failed after 4 attempts", str(caught.exception))
                self.assertEqual(len(logs.output), 4)
                reasons = [record.momoi_fields["reason"] for record in logs.records]
                self.assertTrue(
                    all("backend unavailable" in reason for reason in reasons)
                )
                for output in [str(caught.exception), *logs.output, *reasons]:
                    self.assertNotIn("test-key", output)
                    self.assertNotIn("private spoken text", output)
        self.assertEqual(len(self.requests), 16)
        self.assertFalse((self.root / "audio").exists())

    async def test_transient_http_error_recovers_on_retry(self):
        self.status = 503
        provider = self.provider()
        original = provider._synthesize_once

        async def attempt(text):
            if self.requests:
                self.status = 200
            return await original(text)

        provider._synthesize_once = attempt
        audio = await provider.synthesize("hello")
        self.assertEqual(audio.data, self.audio)
        self.assertEqual(len(self.requests), 2)

    async def test_connection_error_preserves_host_port_and_os_error(self):
        provider = self.provider()
        await self.server.close()
        with self.assertRaises(TTSError) as caught:
            await provider.synthesize("hello")
        detail = str(caught.exception)
        self.assertIn("ClientConnectorError", detail)
        self.assertIn("host=127.0.0.1", detail)
        self.assertIn("port=", detail)
        self.assertIn("os_error=", detail)
        self.assertIn("failed after 4 attempts", detail)

    async def test_cancellation_is_not_retried(self):
        provider = self.provider()
        provider._synthesize_once = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await provider.synthesize("hello")
        provider._synthesize_once.assert_awaited_once()

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
            "providers": "providers.yaml",
            "channels": {
                "primary": "napcat",
                "enabled": {"napcat": {"url": "ws://localhost", "owner_qq": "123"}},
            },
            "context": {},
            "storage": {"database": "data/momoi.sqlite3"},
            "logging": {},
            "tools": {"mcp_config": None},
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(raw))
        catalog = {
            "version": 1,
            "services": {
                "chat": {"adapter": "openai", "base_url": "http://localhost"},
                "voice": {"adapter": "fish"},
            },
            "bindings": {
                "llm": {
                    "service": "chat",
                    "options": {"model": "test", "api_key": "test"},
                },
                "tts": {"service": "voice", "enabled": False, **tts_config},
            },
        }
        (self.root / "providers.yaml").write_text(yaml.safe_dump(catalog))
        return path

    async def test_config_factory_and_runtime_injection(self):
        path = self.write_config({})
        self.assertIsNone(ServiceRegistry(load_config(path).providers).tts)
        settings = {
            "api_key": "config-key",
            "reference_id": "momoi-voice",
            "base_url": str(self.server.make_url("")),
        }
        path = self.write_config({"enabled": True, "options": settings})
        with patch.dict("os.environ", {"MOMOI_TTS_API_KEY": "env-key"}):
            config = load_config(path)
            provider = ServiceRegistry(config.providers).tts
            self.assertEqual(provider.api_key, "config-key")
            from momoi.runtime import MomoiDaemon

            daemon = MomoiDaemon(config)
            try:
                self.assertIsInstance(
                    daemon.bubble_delivery.tts_provider, FishAudioTTSProvider
                )
                self.assertIn(
                    "send_voice",
                    {s["name"] for s in daemon.tool_surface.conversation_specs()},
                )
            finally:
                daemon.store.close()
            audio = await provider.synthesize("provider 测试")
        self.assertEqual(audio.data, self.audio)
        self.assertEqual(self.requests[0][0]["Authorization"], "Bearer config-key")

    def test_invalid_configuration_is_rejected_before_requests(self):
        baseline = {
            "enabled": True,
            "options": {"api_key": "test", "reference_id": "voice"},
        }
        cases = [
            {"provider": "unknown"},
            {"options": {**baseline["options"], "timeout_seconds": 0}},
            {"options": {**baseline["options"], "timeout_seconds": float("nan")}},
            {"options": {**baseline["options"], "max_audio_bytes": True}},
            {"output_dir": ""},
            {"enabled": "true"},
        ]
        for field, value in (
            ("model", "s2.1-pro-fre"),
            ("format", "pcm"),
            ("latency", "fast"),
            ("base_url", "bad-url"),
            ("api_key", ""),
            ("reference_id", 123),
        ):
            cases.append({"options": {**baseline["options"], field: value}})
        with patch.dict("os.environ", {"MOMOI_TTS_API_KEY": ""}):
            for fields in cases:
                with self.subTest(fields=fields):
                    with self.assertRaises(ConfigError):
                        load_config(self.write_config({**baseline, **fields}))
        self.assertEqual(self.requests, [])
