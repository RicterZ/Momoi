import asyncio
import unittest
from unittest.mock import patch

from momoi.config.models import LLMConfig
from momoi.llm.manager import ProviderManager
from momoi.models import ProviderResponse


def _config(api_format: str, model: str = "model") -> LLMConfig:
    return LLMConfig(
        base_url="https://example.com/v1",
        api_key="key",
        model=model,
        max_tokens=100,
        temperature=0,
        timeout_seconds=30,
        max_retries=0,
        api_format=api_format,
    )


class _FakeProvider:
    instances: list["_FakeProvider"] = []

    def __init__(self, config: LLMConfig, _dump_dir: object = None) -> None:
        self.config = config
        self.usage_sink = None
        self.usage_parser = None
        self.thinking_sink = None
        self.entered = 0
        self.closed = 0
        self.request_started = asyncio.Event()
        self.release_request = asyncio.Event()
        self.block = False
        self.instances.append(self)

    async def __aenter__(self) -> "_FakeProvider":
        self.entered += 1
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed += 1

    def update_config(self, config: LLMConfig) -> None:
        self.config = config

    async def complete(self, *_args: object, **_kwargs: object) -> ProviderResponse:
        self.request_started.set()
        if self.block:
            await self.release_request.wait()
        return ProviderResponse([], [])


class ProviderManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeProvider.instances = []

    async def test_protocol_switch_retires_provider_after_its_requests_finish(
        self,
    ) -> None:
        def usage_sink(**_values: object) -> None:
            pass

        def usage_parser(_usage: dict[str, object]) -> None:
            pass

        def thinking_sink(**_values: object) -> None:
            pass

        with (
            patch("momoi.llm.manager.OpenAIProvider", _FakeProvider),
            patch("momoi.llm.manager.AnthropicProvider", _FakeProvider),
        ):
            manager = ProviderManager(_config("openai", "old"))
            manager.usage_sink = usage_sink
            manager.usage_parser = usage_parser
            manager.thinking_sink = thinking_sink
            async with manager:
                old = _FakeProvider.instances[0]
                old.block = True
                request = asyncio.create_task(manager.complete("system", []))
                await old.request_started.wait()

                await manager.update_config(_config("anthropic", "new"))
                new = _FakeProvider.instances[1]

                self.assertEqual(manager.config.api_format, "anthropic")
                self.assertEqual(new.entered, 1)
                self.assertEqual(old.closed, 0)
                self.assertIs(new.usage_sink, usage_sink)
                self.assertIs(new.usage_parser, usage_parser)
                self.assertIs(new.thinking_sink, thinking_sink)

                old.release_request.set()
                await request
                self.assertEqual(old.closed, 1)
                await manager.complete("system", [])
                self.assertTrue(new.request_started.is_set())
            self.assertEqual(new.closed, 1)

    async def test_same_protocol_updates_existing_provider(self) -> None:
        with patch("momoi.llm.manager.OpenAIProvider", _FakeProvider):
            manager = ProviderManager(_config("openai", "old"))
            provider = _FakeProvider.instances[0]
            await manager.update_config(_config("openai", "new"))
        self.assertIs(manager._current.provider, provider)
        self.assertEqual(manager.config.model, "new")

    def test_rejects_unknown_protocol(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be anthropic or openai"):
            ProviderManager(_config("other"))
