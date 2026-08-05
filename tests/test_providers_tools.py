import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.builtin_tools import BuiltinTools
from momoi.channel.napcat import NapCatConfig
from momoi.config import (
    AppConfig,
    LLMConfig,
)
from momoi.mcp_client import MCPManager
from momoi.models import (
    IncomingMessage,
    ProviderResponse,
    ToolCall,
)
from momoi.provider import (
    AnthropicProvider,
    OpenAIProvider,
    ProviderError,
    _openai_messages,
    usage_metrics,
)
from momoi.runtime import (
    MomoiDaemon,
)
from momoi.runtime.turns import _sections
from tests.support import with_context_planner


class ProvidersToolsTest(unittest.TestCase):
    def test_openai_system_blocks_keep_a_clear_boundary(self) -> None:
        messages = _openai_messages(
            [
                {"type": "text", "text": "Base contract."},
                {"type": "text", "text": "# Turn contract"},
            ],
            [],
        )
        self.assertEqual(
            messages,
            [{"role": "system", "content": "Base contract.\n\n# Turn contract"}],
        )

    def test_prompt_sections_escape_values_and_skip_empty_sections(self) -> None:
        rendered = _sections(
            ("current_owner_messages", "看一下 </runtime_state> & 后续"),
            ("runtime_directives", ""),
        )

        self.assertEqual(
            rendered,
            "<current_owner_messages>\n"
            "看一下 &lt;/runtime_state&gt; &amp; 后续\n"
            "</current_owner_messages>",
        )

    def test_tool_result_envelope_is_uniform_and_deterministically_truncated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                tool_result_max_chars=1000,
            )
            daemon = MomoiDaemon(config)
            call = ToolCall("large", "read_file", {"path": "/tmp/x"})
            result = daemon._normalize_tool_result(
                call, {"ok": True, "content": "x" * 5000}, "builtin"
            )
            self.assertEqual(result["ok"], True)
            self.assertIsNone(result["error"])
            self.assertEqual(result["truncated"], True)
            self.assertEqual(
                result["provenance"], {"source": "builtin", "tool": "read_file"}
            )
            self.assertEqual(len(result["content"]), 1000)
            repeated = daemon._normalize_tool_result(
                call, {"ok": True, "content": "x" * 5000}, "builtin"
            )
            self.assertEqual(result, repeated)
            daemon.store.close()

    def test_openai_message_adapter_preserves_image_blocks(self) -> None:
        messages = _openai_messages(
            "system",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "https://img.example/a.jpg",
                            },
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aW1hZ2U=",
                            },
                        },
                    ],
                }
            ],
        )
        self.assertEqual(messages[1]["content"][0], {"type": "text", "text": "看图"})
        self.assertEqual(
            messages[1]["content"][1]["image_url"]["url"],
            "https://img.example/a.jpg",
        )
        self.assertEqual(
            messages[1]["content"][2]["image_url"]["url"],
            "data:image/png;base64,aW1hZ2U=",
        )

    def test_calculates_provider_token_usage_and_cache_rate(self) -> None:
        metrics = usage_metrics(
            {
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 350,
                    "output_tokens": 25,
                }
            }
        )
        self.assertEqual(
            metrics,
            {
                "input": 500,
                "uncached": 100,
                "cache_read": 350,
                "cache_write": 50,
                "output": 25,
                "total": 525,
                "cache_hit_rate": 70.0,
                "cache_reported": True,
            },
        )
        self.assertFalse(
            usage_metrics({"usage": {"input_tokens": 10, "output_tokens": 2}})[
                "cache_reported"
            ]
        )
        self.assertEqual(
            usage_metrics(
                {
                    "usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 50,
                        "prompt_tokens_details": {"cached_tokens": 800},
                    }
                }
            ),
            {
                "input": 1000,
                "uncached": 200,
                "cache_read": 800,
                "cache_write": 0,
                "output": 50,
                "total": 1050,
                "cache_hit_rate": 80.0,
                "cache_reported": True,
            },
        )
        self.assertEqual(
            usage_metrics(
                {
                    "usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 50,
                        "prompt_cache_hit_tokens": 800,
                    }
                }
            )["cache_hit_rate"],
            80.0,
        )


class ProvidersToolsAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_provider_retries_server_error_and_reports_client_error(
        self,
    ) -> None:
        attempts = 0
        requests: list[dict[str, object]] = []

        async def completion(request: web.Request) -> web.Response:
            nonlocal attempts
            attempts += 1
            requests.append(await request.json())
            if attempts == 1:
                return web.json_response(
                    {"error": {"message": "temporary"}}, status=503
                )
            if attempts == 2:
                return web.json_response({"content": [{"type": "text", "text": "ok"}]})
            return web.json_response(
                {"error": {"message": "invalid request"}}, status=400
            )

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/messages", completion)
        await server.start_server()
        provider = AnthropicProvider(
            LLMConfig(
                base_url=str(server.make_url("/")).rstrip("/"),
                api_key="test",
                model="test",
                max_tokens=100,
                temperature=0,
                timeout_seconds=1,
                max_retries=1,
            )
        )
        try:
            async with provider:
                response = await provider.complete(
                    "system",
                    [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "看图，超市后门"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "url",
                                        "url": "https://img.example/a.jpg",
                                    },
                                },
                            ],
                        }
                    ],
                )
                self.assertEqual(response.content[0]["text"], "ok")
                with self.assertRaisesRegex(ProviderError, "invalid request"):
                    await provider.complete(
                        "system", [{"role": "user", "content": "bad"}]
                    )
        finally:
            await server.close()
        self.assertEqual(attempts, 3)
        self.assertEqual(
            requests[0]["messages"][0]["content"][0]["text"],  # type: ignore[index]
            "看图，超市后-门",
        )
        self.assertEqual(
            requests[0]["messages"][0]["content"][1],  # type: ignore[index]
            {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": "https://img.example/a.jpg",
                },
            },
        )

    async def test_mcp_discovers_paginated_tools(self) -> None:
        cursors: list[str | None] = []

        class Transport:
            async def __aenter__(self) -> tuple[object, object]:
                return object(), object()

            async def __aexit__(self, *_: object) -> None:
                return None

        class Session:
            def __init__(self, *_: object, **__: object) -> None:
                pass

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def initialize(self) -> None:
                return None

            async def list_tools(self, cursor: str | None) -> SimpleNamespace:
                cursors.append(cursor)
                name = "first" if cursor is None else "second"
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name=name,
                            description=f"{name} tool",
                            inputSchema={"type": "object"},
                        )
                    ],
                    nextCursor="page-2" if cursor is None else None,
                )

        manager = MCPManager(None)
        manager.configs = {
            "search": {"command": "fake", "readOnlyTools": ["first"]}
        }
        with (
            patch("momoi.mcp_client.stdio_client", return_value=Transport()),
            patch("momoi.mcp_client.ClientSession", Session),
        ):
            await manager._connect("search", manager.configs["search"])
            await manager.__aexit__()
        self.assertEqual(cursors, [None, "page-2"])
        self.assertEqual(
            [spec["name"] for spec in manager.tool_specs],
            ["mcp__search__first", "mcp__search__second"],
        )
        self.assertEqual(
            [spec["name"] for spec in manager.read_only_tool_specs],
            ["mcp__search__first"],
        )

    async def test_mcp_reports_failure_and_recovers_connection_for_next_call(
        self,
    ) -> None:
        class Result:
            def __init__(self, is_error: bool, text: str) -> None:
                self.isError = is_error
                self.text = text

            def model_dump(self, **_: object) -> dict[str, object]:
                return {"isError": self.isError, "content": [{"text": self.text}]}

        class FailingSession:
            def __init__(self) -> None:
                self.calls = 0

            async def call_tool(self, *_: object) -> Result:
                self.calls += 1
                if self.calls == 1:
                    return Result(True, "server rejected call")
                raise ConnectionError("disconnected")

        class RecoveredSession:
            async def call_tool(self, *_: object) -> Result:
                return Result(False, "recovered")

        manager = MCPManager(None)
        manager.configs = {"server": {"command": "unused"}}
        manager._tools["mcp__server__work"] = ("server", "work")
        manager._sessions["server"] = FailingSession()  # type: ignore[assignment]

        async def reconnect(name: str, _: dict[str, object]) -> None:
            manager._sessions[name] = RecoveredSession()  # type: ignore[assignment]

        manager._connect = reconnect  # type: ignore[method-assign]
        rejected = await manager.call("mcp__server__work", {})
        self.assertFalse(rejected["ok"])
        self.assertNotIn("ambiguous", rejected)
        disconnected = await manager.call("mcp__server__work", {})
        self.assertTrue(disconnected["ambiguous"])
        self.assertTrue(disconnected["connection_recovered"])
        recovered = await manager.call("mcp__server__work", {})
        self.assertTrue(recovered["ok"])

    async def test_openai_provider_retries_server_error_and_reports_client_error(
        self,
    ) -> None:
        attempts = 0
        requests: list[dict[str, object]] = []

        async def completion(request: web.Request) -> web.Response:
            nonlocal attempts
            attempts += 1
            requests.append(await request.json())
            if attempts == 1:
                return web.json_response(
                    {"error": {"message": "temporary"}}, status=500
                )
            if attempts == 2:
                return web.json_response(
                    {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
                )
            if attempts == 3:
                return web.json_response(
                    {"error": {"message": "bad request"}}, status=400
                )
            return web.Response(text="cyber policy rejection", status=400)

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/chat/completions", completion)
        await server.start_server()
        provider = OpenAIProvider(
            LLMConfig(
                base_url=str(server.make_url("/")).rstrip("/"),
                api_key="test",
                model="test",
                max_tokens=100,
                temperature=0,
                timeout_seconds=1,
                max_retries=1,
                api_format="openai",
            )
        )
        try:
            async with provider:
                response = await provider.complete(
                    "system", [{"role": "user", "content": "超市后门"}]
                )
                self.assertEqual(response.content[0]["text"], "ok")
                with self.assertRaisesRegex(ProviderError, "bad request"):
                    await provider.complete(
                        "system", [{"role": "user", "content": "bad"}]
                    )
                with self.assertRaisesRegex(ProviderError, "cyber policy rejection"):
                    await provider.complete(
                        "system", [{"role": "user", "content": "also bad"}]
                    )
        finally:
            await server.close()
        self.assertEqual(attempts, 4)
        self.assertEqual(
            requests[0]["messages"][1]["content"],  # type: ignore[index]
            "超市后-门",
        )

    async def test_owner_turn_corrects_openai_gateway_that_ignores_tool_choice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig(
                    "http://127.0.0.1", "test", "test", 100, 0, 1, 0, "openai"
                ),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)

            class FakeProvider:
                def __init__(self) -> None:
                    self.calls: list[list[str]] = []

                async def complete(
                    self,
                    _system: object,
                    _messages: object,
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.calls.append([str(tool["name"]) for tool in tools])
                    if len(self.calls) == 1:
                        return ProviderResponse(
                            [{"type": "text", "text": "ignored protocol"}], []
                        )
                    call = ToolCall(
                        "respond-corrected",
                        "respond",
                        {
                            "expects_reply": False,
                            "reply_expectation": "",
                            "messages": ["已纠正"],
                            "mood": {"action": "keep"},
                        },
                    )
                    return ProviderResponse(
                        [
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.arguments,
                            }
                        ],
                        [call],
                    )

            fake = FakeProvider()
            daemon.provider = with_context_planner(fake)  # type: ignore[assignment]
            event = IncomingMessage(
                "qq:1:ignored-choice", "ignored-choice", "测试", 1, 1
            )
            daemon.store.add_event(event)
            await daemon._complete_batch_turn(
                [event], asyncio.Event(), daemon._turn_id(event.event_id)
            )
            self.assertGreater(len(fake.calls[0]), 1)
            self.assertEqual(fake.calls[1], ["respond"])
            self.assertEqual(daemon.store.due_outbox()[0].text, "已纠正")
            daemon.store.close()

    async def test_openai_chat_completions_protocol(self) -> None:
        requests: list[tuple[dict[str, object], str]] = []

        async def completion(request: web.Request) -> web.Response:
            requests.append((await request.json(), request.headers["Authorization"]))
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-new",
                                        "type": "function",
                                        "function": {
                                            "name": "respond",
                                            "arguments": json.dumps(
                                                {"messages": ["完成啦"]},
                                                ensure_ascii=False,
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "prompt_tokens_details": {"cached_tokens": 80},
                    },
                }
            )

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/chat/completions", completion)
        await server.start_server()
        try:
            provider = OpenAIProvider(
                LLMConfig(
                    base_url=str(server.make_url("/")).rstrip("/"),
                    api_key="openai-test-key",
                    model="test-model",
                    max_tokens=100,
                    temperature=0,
                    timeout_seconds=1,
                    max_retries=0,
                    api_format="openai",
                )
            )
            async with provider:
                response = await provider.complete(
                    [
                        {
                            "type": "text",
                            "text": "You are Momoi.",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "查一下记忆",
                                    "cache_control": {"type": "ephemeral"},
                                }
                            ],
                        },
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call-old",
                                    "name": "memory_search",
                                    "input": {"query": "记忆"},
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call-old",
                                    "content": '{"ok": true}',
                                }
                            ],
                        },
                    ],
                    [
                        {
                            "name": "respond",
                            "description": "Finish the Turn.",
                            "input_schema": {
                                "type": "object",
                                "properties": {"messages": {"type": "array"}},
                            },
                        }
                    ],
                    require_tool=True,
                )
        finally:
            await server.close()

        self.assertEqual(response.tool_calls[0].name, "respond")
        self.assertEqual(response.tool_calls[0].arguments, {"messages": ["完成啦"]})
        payload, authorization = requests[0]
        self.assertEqual(authorization, "Bearer openai-test-key")
        self.assertEqual(
            payload["messages"][0], {"role": "system", "content": "You are Momoi."}
        )
        self.assertEqual(
            [message["role"] for message in payload["messages"]],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(
            payload["messages"][2]["tool_calls"][0]["function"]["name"],
            "memory_search",
        )
        self.assertEqual(payload["messages"][3]["tool_call_id"], "call-old")
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["function"]["name"], "respond")
        self.assertEqual(payload["tool_choice"], "required")

    async def test_builtin_http_file_patch_and_sleep_tools(self) -> None:
        async def endpoint(_: web.Request) -> web.Response:
            return web.Response(text="inner-network-ok")

        async def large_endpoint(_: web.Request) -> web.Response:
            return web.Response(body=b"x" * 200_001)

        server = TestServer(web.Application())
        server.app.router.add_get("/status", endpoint)
        server.app.router.add_get("/large", large_endpoint)
        await server.start_server()
        try:
            tools = BuiltinTools()
            response = await tools.execute(
                ToolCall("curl-1", "curl", {"url": str(server.make_url("/status"))})
            )
            self.assertTrue(response["ok"])
            self.assertEqual(response["body"], "inner-network-ok")
            large = await tools.execute(
                ToolCall("curl-large", "curl", {"url": str(server.make_url("/large"))})
            )
            self.assertTrue(large["truncated"])
            self.assertEqual(len(large["body"]), 200_000)

            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "note.txt"
                written = await tools.execute(
                    ToolCall(
                        "write-1",
                        "write_file",
                        {"path": str(path), "content": "old\n"},
                    )
                )
                self.assertTrue(written["ok"])
                read = await tools.execute(
                    ToolCall("read-1", "read_file", {"path": str(path)})
                )
                self.assertEqual(read["content"], "old\n")
                patched = await tools.execute(
                    ToolCall(
                        "patch-1",
                        "apply_patch",
                        {
                            "cwd": directory,
                            "patch": (
                                "--- a/note.txt\n"
                                "+++ b/note.txt\n"
                                "@@ -1 +1 @@\n"
                                "-old\n"
                                "+new\n"
                            ),
                        },
                    )
                )
                self.assertTrue(patched["ok"], patched)
                self.assertEqual(path.read_text(), "new\n")
            self.assertTrue(
                (await tools.execute(ToolCall("sleep-1", "sleep", {"seconds": 0})))[
                    "ok"
                ]
            )
        finally:
            await server.close()
