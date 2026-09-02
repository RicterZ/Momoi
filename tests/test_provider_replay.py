import unittest
from unittest.mock import AsyncMock, call, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.config import LLMConfig
from momoi.llm.anthropic import AnthropicProvider
from momoi.llm.errors import ProviderError
from momoi.llm.openai import OpenAIProvider


def _config(base_url: str, api_format: str) -> LLMConfig:
    return LLMConfig(
        base_url=base_url,
        api_key="test",
        model="test",
        max_tokens=100,
        temperature=0,
        timeout_seconds=1,
        max_retries=1,
        api_format=api_format,
    )


class ProviderReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_invalid_json_retries_then_succeeds(self):
        attempts = 0

        async def completion(_: web.Request) -> web.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return web.Response(text="{", content_type="application/json")
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            )

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/chat/completions", completion)
        await server.start_server()
        try:
            provider = OpenAIProvider(
                _config(str(server.make_url("/")).rstrip("/"), "openai")
            )
            sleep = AsyncMock()
            with patch("momoi.llm.transport.asyncio.sleep", new=sleep):
                async with provider:
                    response = await provider.complete(
                        "system", [{"role": "user", "content": "test"}]
                    )
            self.assertEqual(response.content[0]["text"], "ok")
            self.assertEqual(attempts, 2)
            self.assertEqual(sleep.await_args_list, [call(1)])
        finally:
            await server.close()

    async def test_anthropic_invalid_json_retries_then_succeeds(self):
        attempts = 0

        async def completion(_: web.Request) -> web.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return web.Response(text="{", content_type="application/json")
            return web.json_response({"content": [{"type": "text", "text": "ok"}]})

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/messages", completion)
        await server.start_server()
        try:
            provider = AnthropicProvider(
                _config(str(server.make_url("/")).rstrip("/"), "anthropic")
            )
            sleep = AsyncMock()
            with patch("momoi.llm.transport.asyncio.sleep", new=sleep):
                async with provider:
                    response = await provider.complete(
                        "system", [{"role": "user", "content": "test"}]
                    )
            self.assertEqual(response.content[0]["text"], "ok")
            self.assertEqual(attempts, 2)
            self.assertEqual(sleep.await_args_list, [call(1)])
        finally:
            await server.close()

    async def test_rate_limit_and_empty_content_remain_errors(self):
        attempts = 0

        async def completion(_: web.Request) -> web.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return web.json_response(
                    {"error": {"message": "rate limited"}}, status=429
                )
            return web.json_response({"content": []})

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/messages", completion)
        await server.start_server()
        try:
            provider = AnthropicProvider(
                _config(str(server.make_url("/")).rstrip("/"), "anthropic")
            )
            async with provider:
                with self.assertRaisesRegex(ProviderError, "rate limited"):
                    await provider.complete(
                        "system", [{"role": "user", "content": "rate"}]
                    )
                with self.assertRaisesRegex(ProviderError, "no text content"):
                    await provider.complete(
                        "system", [{"role": "user", "content": "empty"}]
                    )
            self.assertEqual(attempts, 2)
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
