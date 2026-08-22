import asyncio
import json
import logging
import tempfile
import unittest
from contextlib import contextmanager
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
    ThinkingConfig,
)
from momoi.mcp_client import MCPManager
from momoi.models import (
    IncomingMessage,
    ProviderResponse,
    ToolCall,
)
from momoi.logging_context import TRACE, log_context
from momoi.provider import (
    AnthropicProvider,
    OpenAIProvider,
    ProviderError,
    _compact_response_text,
    _log_tool_schema,
    _openai_messages,
    _redact_dump_media,
    usage_metrics,
)
from momoi.runtime import (
    MomoiDaemon,
)
from momoi.runtime.turn_support import (
    pack_user_context,
    sections,
    truncate_tool_result_json,
)
from tests.support import with_context_planner


@contextmanager
def _provider_trace_logs():
    logger = logging.getLogger("momoi.provider")
    previous = logger.level
    logger.setLevel(TRACE)
    try:
        yield
    finally:
        logger.setLevel(previous)


class ProvidersToolsTest(unittest.TestCase):
    def test_tool_schema_diagnostic_hashes_actual_wire_shape(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "demo",
                    "description": "Demo tool",
                    "parameters": {"type": "object"},
                },
            }
        ]
        with (
            _provider_trace_logs(),
            self.assertLogs("momoi.provider", level=TRACE) as logs,
        ):
            _log_tool_schema("openai", tools)

        record = next(
            item
            for item in logs.records
            if getattr(item, "momoi_event", "") == "llm_tool_schema"
        )
        self.assertEqual(record.momoi_fields["tool_count"], 1)
        self.assertEqual(record.momoi_fields["tool_names"], ["demo"])
        self.assertGreater(record.momoi_fields["tool_schema_tokens"], 0)
        self.assertRegex(
            record.momoi_fields["tool_schema_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_context_truncation_keeps_error_envelope_valid(self) -> None:
        rendered = truncate_tool_result_json(
            json.dumps(
                {
                    "ok": False,
                    "error": "upstream_error",
                    "message": "specific reason",
                    "result": {"content": "x" * 5000},
                }
            ),
            1000,
        )
        parsed = json.loads(rendered)
        self.assertEqual(parsed["error"], "upstream_error")
        self.assertEqual(parsed["message"], "specific reason")
        self.assertTrue(parsed["truncated"])

    def test_openai_adapter_orders_tool_result_before_correction_text(self) -> None:
        messages = _openai_messages(
            "system",
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "reasoning",
                            "text": "先核对计划结果",
                        },
                        {
                            "type": "tool_use",
                            "id": "plan-1",
                            "name": "submit_context_plan",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "plan-1",
                            "content": '{"ok":false}',
                            "is_error": True,
                        },
                        {"type": "text", "text": "Call the tool again."},
                    ],
                },
            ],
        )
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "assistant", "tool", "user"],
        )
        self.assertEqual(
            messages[1]["reasoning_content"],
            "先核对计划结果",
        )

    def test_compacts_structured_response_text_for_single_line_logs(self) -> None:
        self.assertEqual(
            _compact_response_text('{\n  "version": 1,\n  "items": ["a", "b"]\n}'),
            '{"version":1,"items":["a","b"]}',
        )
        self.assertEqual(_compact_response_text("普通\n文本"), '"普通\\n文本"')

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
        rendered = sections(
            ("current_owner_messages", "看一下 </runtime_state> & 后续"),
            ("runtime_directives", ""),
        )

        self.assertEqual(
            rendered,
            "<current_owner_messages>\n"
            "看一下 &lt;/runtime_state&gt; &amp; 后续\n"
            "</current_owner_messages>",
        )

    def test_user_pack_puts_stable_identity_before_clock_and_task(self) -> None:
        rendered = pack_user_context(
            ("pending_owner_reply", '{"waiting":true}'),
            ("runtime_state", "now"),
            ("conversation_state", '{"busy":false}'),
            ("long_term_memories", "喜欢短回复"),
            ("recent_conversation", "user: hi"),
        )
        self.assertNotIn("<emotion_catalog>", rendered)
        self.assertLess(
            rendered.index("<long_term_memories>"),
            rendered.index("<recent_conversation>"),
        )
        self.assertLess(
            rendered.index("<recent_conversation>"),
            rendered.index("<conversation_state>"),
        )
        self.assertLess(
            rendered.index("<conversation_state>"),
            rendered.index("<runtime_state>"),
        )
        self.assertLess(
            rendered.index("<runtime_state>"),
            rendered.index("<pending_owner_reply>"),
        )

    def test_user_pack_keeps_query_specific_recall_after_recent_turns(self) -> None:
        rendered = pack_user_context(
            ("recall_memories", "召回的事实"),
            ("recall_status", "queries=棕榈\nmisses=棕榈"),
            ("reflection_memories", "今日学习"),
            ("episode_directory", "旧话题"),
            ("recent_turns", '{"version":1,"turns":[]}'),
            ("long_term_memories", "喜欢短回复"),
            ("active_goals", "喝水"),
            ("current_owner_messages", "在吗"),
        )
        self.assertLess(
            rendered.index("<long_term_memories>"),
            rendered.index("<active_goals>"),
        )
        self.assertLess(
            rendered.index("<active_goals>"),
            rendered.index("<recent_turns>"),
        )
        self.assertLess(
            rendered.index("<recent_turns>"),
            rendered.index("<episode_directory>"),
        )
        self.assertLess(
            rendered.index("<recent_turns>"),
            rendered.index("<recall_memories>"),
        )
        self.assertLess(
            rendered.index("<recall_memories>"),
            rendered.index("<recall_status>"),
        )
        self.assertLess(
            rendered.index("<recent_turns>"),
            rendered.index("<reflection_memories>"),
        )
        self.assertLess(
            rendered.index("<recent_turns>"),
            rendered.index("<current_owner_messages>"),
        )
        with self.assertRaisesRegex(ValueError, "unknown user context section"):
            pack_user_context(("not_a_section", "x"))

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
            failed = daemon._normalize_tool_result(
                call,
                {
                    "ok": False,
                    "error": "patch_failed",
                    "message": "The patch context did not match the file.",
                    "content": "x" * 5000,
                },
                "builtin",
            )
            self.assertEqual(
                failed["message"],
                "The patch context did not match the file.",
            )
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

    def test_prompt_dump_redacts_embedded_image_bytes(self) -> None:
        payload = {
            "anthropic": {"type": "base64", "data": "aW1hZ2U="},
            "openai": "data:image/png;base64,aW1hZ2U=",
        }
        redacted = _redact_dump_media(payload)
        self.assertEqual(
            redacted,
            {
                "anthropic": {
                    "type": "base64",
                    "data": "[omitted 8 base64 chars]",
                },
                "openai": "data:image/png;base64,[omitted 8 base64 chars]",
            },
        )
        self.assertEqual(payload["anthropic"]["data"], "aW1hZ2U=")

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
                        "output_tokens": 50,
                        "prompt_tokens_details": {"cached_tokens": 800},
                    }
                }
            )["output"],
            50,
        )
        ignored_deepseek_fields = usage_metrics(
            {
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 20,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_cache_miss_tokens": 200,
                }
            }
        )
        self.assertEqual(ignored_deepseek_fields["cache_read"], 0)
        self.assertEqual(ignored_deepseek_fields["uncached"], 50)
        self.assertEqual(ignored_deepseek_fields["output"], 20)


class ProvidersToolsAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_openai_provider_dumps_final_request_at_trace(self) -> None:
        requests: list[dict[str, object]] = []

        async def completion(request: web.Request) -> web.Response:
            requests.append(await request.json())
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            )

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/chat/completions", completion)
        await server.start_server()
        with tempfile.TemporaryDirectory() as directory:
            dump_dir = Path(directory) / "llm-dumps"
            provider = OpenAIProvider(
                LLMConfig(
                    base_url=str(server.make_url("/")).rstrip("/"),
                    api_key="test",
                    model="test",
                    max_tokens=100,
                    temperature=0,
                    timeout_seconds=1,
                    max_retries=0,
                    api_format="openai",
                    thinking=ThinkingConfig(
                        effort="high",
                        stages={"context_plan": "low"},
                    ),
                ),
                dump_dir,
            )
            try:
                async with provider:
                    with _provider_trace_logs():
                        with log_context(stage="context_plan"):
                            await provider.complete(
                                "system",
                                [{"role": "user", "content": "测试"}],
                                [
                                    {
                                        "name": "respond",
                                        "input_schema": {"type": "object"},
                                    }
                                ],
                                require_tool=True,
                            )
            finally:
                await server.close()
            dumps = list(dump_dir.glob("*.json"))
            self.assertEqual(len(dumps), 1)
            dumped = json.loads(dumps[0].read_text())
            self.assertEqual(dumped["provider"], "openai")
            self.assertEqual(dumped["payload"], requests[0])
            self.assertEqual(dumped["payload"]["messages"][0]["role"], "system")
            self.assertIn("tools", dumped["payload"])
            self.assertEqual(dumped["payload"]["reasoning_effort"], "low")
            self.assertEqual(
                dumped["payload"]["thinking"],
                {"type": "enabled"},
            )
            self.assertNotIn("temperature", dumped["payload"])
            self.assertEqual(
                dumped["response"],
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

    async def test_openai_provider_dumps_thinking_in_the_same_file(self) -> None:
        async def completion(_: web.Request) -> web.Response:
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "先核对记忆再改 activation",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "respond",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/chat/completions", completion)
        await server.start_server()
        with tempfile.TemporaryDirectory() as directory:
            dump_dir = Path(directory) / "llm-dumps"
            provider = OpenAIProvider(
                LLMConfig(
                    base_url=str(server.make_url("/")).rstrip("/"),
                    api_key="test",
                    model="test",
                    max_tokens=100,
                    temperature=0,
                    timeout_seconds=1,
                    max_retries=0,
                    api_format="openai",
                ),
                dump_dir,
            )
            try:
                async with provider:
                    with _provider_trace_logs():
                        response = await provider.complete(
                            "system",
                            [{"role": "user", "content": "测试"}],
                            [{"name": "respond", "input_schema": {"type": "object"}}],
                            require_tool=True,
                        )
            finally:
                await server.close()
            dumps = list(dump_dir.glob("*.json"))
            self.assertEqual(len(dumps), 1)
            dumped = json.loads(dumps[0].read_text())
            self.assertNotIn("thinking", dumped)
            self.assertEqual(
                dumped["response"]["choices"][0]["message"]["reasoning_content"],
                "先核对记忆再改 activation",
            )
            self.assertEqual(response.reasoning, "先核对记忆再改 activation")

    async def test_openai_provider_skips_dump_without_trace(self) -> None:
        async def completion(_: web.Request) -> web.Response:
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            )

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/chat/completions", completion)
        await server.start_server()
        with tempfile.TemporaryDirectory() as directory:
            dump_dir = Path(directory) / "llm-dumps"
            provider = OpenAIProvider(
                LLMConfig(
                    base_url=str(server.make_url("/")).rstrip("/"),
                    api_key="test",
                    model="test",
                    max_tokens=100,
                    temperature=0,
                    timeout_seconds=1,
                    max_retries=0,
                    api_format="openai",
                ),
                dump_dir,
            )
            try:
                async with provider:
                    await provider.complete(
                        "system",
                        [{"role": "user", "content": "测试"}],
                    )
            finally:
                await server.close()
            self.assertEqual(list(dump_dir.glob("*.json")), [])

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
                thinking=ThinkingConfig(
                    effort="high",
                    stages={"heartbeat_plan": "low"},
                ),
            )
        )
        try:
            async with provider:
                with log_context(stage="heartbeat_plan"):
                    response = await provider.complete(
                        "system",
                        [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "看图，测试入口"},
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
            requests[0]["output_config"],
            {"effort": "low"},
        )
        self.assertNotIn("temperature", requests[0])
        self.assertEqual(
            requests[0]["messages"][0]["content"][0]["text"],  # type: ignore[index]
            "看图，测试入口",
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
                name = "zeta" if cursor is None else "alpha"
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
            "search": {"command": "fake", "readOnlyTools": ["zeta"]}
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
            ["mcp__search__alpha", "mcp__search__zeta"],
        )
        self.assertEqual(manager.capability("mcp__search__zeta"), "read")
        self.assertEqual(
            manager.capability("mcp__search__alpha"), "external_effect"
        )

    async def test_mcp_enabled_tools_filters_registered_surface(self) -> None:
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

            async def list_tools(self, _: str | None) -> SimpleNamespace:
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name=name,
                            description=f"{name} tool",
                            inputSchema={"type": "object"},
                        )
                        for name in ("first", "second", "third")
                    ],
                    nextCursor=None,
                )

        for enabled, expected in (
            (["second"], ["mcp__search__second"]),
            (["mcp__search__first"], ["mcp__search__first"]),
            ([], []),
        ):
            manager = MCPManager(None)
            config = {
                "command": "fake",
                "enabled_tools": enabled,
            }
            with (
                patch("momoi.mcp_client.stdio_client", return_value=Transport()),
                patch("momoi.mcp_client.ClientSession", Session),
            ):
                await manager._connect("search", config)
                await manager.__aexit__()
            self.assertEqual(
                [spec["name"] for spec in manager.tool_specs],
                expected,
            )

    async def test_mcp_recreates_invalid_session_without_stopping_other_servers(
        self,
    ) -> None:
        generations: dict[str, int] = {}
        calls: dict[tuple[str, int], int] = {}
        owners: dict[str, asyncio.Task[object] | None] = {}
        test_case = self

        class Result:
            def __init__(self, is_error: bool = False) -> None:
                self.isError = is_error

            def model_dump(self, **_: object) -> dict[str, object]:
                return {"isError": self.isError, "content": [{"text": "ok"}]}

        class Transport:
            def __init__(self, server: str, generation: int) -> None:
                self.server = server
                self.generation = generation
                self.owner: asyncio.Task[object] | None = None

            async def __aenter__(self) -> tuple[object, object]:
                self.owner = asyncio.current_task()
                owners[f"{self.server}:{self.generation}"] = self.owner
                return (self.server, self.generation), object()

            async def __aexit__(self, *_: object) -> None:
                test_case.assertIs(asyncio.current_task(), self.owner)
                if self.server == "stale" and self.generation == 1:
                    raise asyncio.CancelledError("stale session cleanup")

        class Session:
            def __init__(self, read: tuple[str, int], *_: object, **__: object) -> None:
                self.server, self.generation = read
                self.owner: asyncio.Task[object] | None = None

            async def __aenter__(self) -> "Session":
                self.owner = asyncio.current_task()
                return self

            async def __aexit__(self, *_: object) -> None:
                test_case.assertIs(asyncio.current_task(), self.owner)

            async def initialize(self) -> None:
                return None

            async def list_tools(self, _: str | None) -> SimpleNamespace:
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name="work",
                            description="work",
                            inputSchema={"type": "object"},
                        )
                    ],
                    nextCursor=None,
                )

            async def call_tool(self, *_: object) -> Result:
                key = (self.server, self.generation)
                calls[key] = calls.get(key, 0) + 1
                if key == ("stale", 1):
                    if calls[key] == 1:
                        return Result(True)
                    raise ConnectionError("missing session")
                return Result()

        def make_transport(parameters: object) -> Transport:
            server = str(getattr(parameters, "command"))
            generations[server] = generations.get(server, 0) + 1
            return Transport(server, generations[server])

        manager = MCPManager(None)
        manager.configs = {
            "stale": {"command": "stale"},
            "healthy": {"command": "healthy"},
        }
        with (
            patch("momoi.mcp_client.stdio_client", side_effect=make_transport),
            patch("momoi.mcp_client.ClientSession", Session),
        ):
            async with manager:
                rejected = await manager.call("mcp__stale__work", {})
                stale = await manager.call("mcp__stale__work", {})
                healthy = await manager.call("mcp__healthy__work", {})
                recovered = await manager.call("mcp__stale__work", {})

        self.assertFalse(rejected["ok"])
        self.assertNotIn("ambiguous", rejected)
        self.assertEqual(rejected["error"], "mcp_tool_error")
        self.assertEqual(rejected["message"], "ok")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"], "mcp_transport_error")
        self.assertIn("missing session", stale["message"])
        self.assertTrue(stale["ambiguous"])
        self.assertTrue(stale["connection_recovered"])
        self.assertTrue(healthy["ok"])
        self.assertTrue(recovered["ok"])
        self.assertEqual(generations["stale"], 2)
        self.assertEqual(generations["healthy"], 1)
        self.assertIsNot(owners["stale:1"], owners["healthy:1"])

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
                with self.assertLogs("momoi.provider", level="DEBUG") as logs:
                    response = await provider.complete(
                        "system", [{"role": "user", "content": "测试入口"}]
                    )
                self.assertEqual(response.content[0]["text"], "ok")
                requests = [
                    record
                    for record in logs.records
                    if getattr(record, "momoi_event", "") == "llm_request"
                ]
                retries = [
                    record
                    for record in logs.records
                    if getattr(record, "momoi_event", "") == "llm_retry"
                ]
                self.assertEqual(requests[0].momoi_fields["model"], "test")
                self.assertEqual(requests[0].momoi_fields["attempt"], 1)
                self.assertEqual(requests[1].momoi_fields["attempt"], 2)
                self.assertEqual(retries[0].momoi_fields["attempt_max"], 2)
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

    async def test_openai_provider_retries_unusable_success_response(self) -> None:
        attempts = 0

        async def completion(_request: web.Request) -> web.Response:
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                return web.json_response(
                    {"choices": [], "error": {"message": "upstream overloaded"}}
                )
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            )

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
                max_retries=3,
                api_format="openai",
            )
        )
        try:
            async with provider:
                with self.assertLogs("momoi.provider", level="WARNING") as logs:
                    response = await provider.complete(
                        "system", [{"role": "user", "content": "测试入口"}]
                    )
                self.assertEqual(response.content[0]["text"], "ok")
                unusable = [
                    record
                    for record in logs.records
                    if getattr(record, "momoi_event", "")
                    == "llm_response_unusable"
                ]
                self.assertEqual(len(unusable), 3)
                self.assertIn("upstream overloaded", unusable[0].momoi_fields["body"])
                self.assertIn("body", unusable[0].momoi_fields)
        finally:
            await server.close()
        self.assertEqual(attempts, 4)

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
                    if len(self.calls) == 2:
                        call = ToolCall(
                            "send-corrected",
                            "send_message",
                            {"messages": ["已纠正"]},
                        )
                    else:
                        call = ToolCall(
                            "respond-corrected",
                            "respond",
                            {
                                "expects_reply": False,
                                "reply_expectation": "",
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
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
            self.assertEqual(fake.calls[1], fake.calls[0])
            self.assertEqual(fake.calls[2], fake.calls[0])
            self.assertEqual(daemon.store.due_outbox()[0].text, "已纠正")
            daemon.store.close()

    async def test_owner_turn_returns_argument_parse_error_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig(
                    "http://127.0.0.1", "test", "test", 100, 0, 1, 0, "openai"
                ),
                channel=NapCatConfig(
                    "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                ),
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
                calls = 0

                async def complete(
                    self,
                    _system: object,
                    messages: object,
                    _tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    rendered = json.dumps(messages, ensure_ascii=False)
                    if self.calls == 1:
                        call = ToolCall(
                            "bad-json",
                            "send_message",
                            {},
                            "invalid_tool_arguments_json",
                        )
                    elif self.calls == 2:
                        self_test.assertIn(
                            "invalid_tool_arguments_json", rendered
                        )
                        self_test.assertIn("reasoning", rendered)
                        self_test.assertIn("先纠正参数", rendered)
                        call = ToolCall(
                            "corrected-message",
                            "send_message",
                            {"messages": ["参数已纠正"]},
                        )
                    else:
                        call = ToolCall(
                            "corrected-response",
                            "respond",
                            {
                                "expects_reply": False,
                                "reply_expectation": "",
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
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
                        reasoning=(
                            "先纠正参数"
                            if self.calls == 1
                            else ""
                        ),
                    )

            self_test = self
            fake = FakeProvider()
            daemon.provider = with_context_planner(fake)  # type: ignore[assignment]
            event = IncomingMessage("qq:1:bad-json", "bad-json", "测试", 1, 1)
            daemon.store.add_event(event)
            await daemon._complete_batch_turn(
                [event], asyncio.Event(), daemon._turn_id(event.event_id)
            )
            self.assertEqual(daemon.store.due_outbox()[0].text, "参数已纠正")
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
                                                {
                                                    "expects_reply": False,
                                                    "reply_expectation": "",
                                                    "mood": {"decision": "unchanged"},
                                                },
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
                                "properties": {
                                    "expects_reply": {"type": "boolean"},
                                    "reply_expectation": {"type": "string"},
                                    "mood": {"type": "object"},
                                },
                            },
                        }
                    ],
                    require_tool=True,
                )
            provider_without_tool_choice = OpenAIProvider(
                LLMConfig(
                    base_url=str(server.make_url("/")).rstrip("/"),
                    api_key="openai-test-key",
                    model="thinking-model",
                    max_tokens=100,
                    temperature=0,
                    timeout_seconds=1,
                    max_retries=0,
                    api_format="openai",
                    tool_choice=False,
                )
            )
            async with provider_without_tool_choice:
                await provider_without_tool_choice.complete(
                    "system",
                    [{"role": "user", "content": "测试"}],
                    [
                        {
                            "name": "respond",
                            "description": "Finish the Turn.",
                            "input_schema": {"type": "object"},
                        }
                    ],
                    require_tool=True,
                )
        finally:
            await server.close()

        self.assertEqual(response.tool_calls[0].name, "respond")
        self.assertEqual(
            response.tool_calls[0].arguments,
            {
                "expects_reply": False,
                "reply_expectation": "",
                "mood": {"decision": "unchanged"},
            },
        )
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
        self.assertNotIn("tool_choice", requests[1][0])

    async def test_openai_invalid_tool_arguments_keep_parse_error(self) -> None:
        async def completion(_: web.Request) -> web.Response:
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "bad-arguments",
                                        "type": "function",
                                        "function": {
                                            "name": "respond",
                                            "arguments": "{not-json",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        server = TestServer(web.Application())
        server.app.router.add_post("/v1/chat/completions", completion)
        await server.start_server()
        try:
            provider = OpenAIProvider(
                LLMConfig(
                    base_url=str(server.make_url("/")).rstrip("/"),
                    api_key="test",
                    model="test",
                    max_tokens=100,
                    temperature=0,
                    timeout_seconds=1,
                    max_retries=0,
                    api_format="openai",
                )
            )
            async with provider:
                response = await provider.complete(
                    "system",
                    [{"role": "user", "content": "test"}],
                    [
                        {
                            "name": "respond",
                            "input_schema": {"type": "object"},
                        }
                    ],
                )
        finally:
            await server.close()
        self.assertEqual(
            response.tool_calls[0].argument_error,
            "invalid_tool_arguments_json",
        )

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
                workspace_tools = BuiltinTools(Path(directory))
                path = Path(directory) / "note.txt"
                written = await workspace_tools.execute(
                    ToolCall(
                        "write-1",
                        "write_file",
                        {"path": "note.txt", "content": "old\n"},
                    )
                )
                self.assertTrue(written["ok"])
                read = await workspace_tools.execute(
                    ToolCall("read-1", "read_file", {"path": "note.txt"})
                )
                self.assertEqual(read["content"], "old\n")
                listed = await workspace_tools.execute(
                    ToolCall("list-1", "list_dir", {"path": "."})
                )
                self.assertTrue(listed["ok"], listed)
                self.assertEqual(
                    [entry["name"] for entry in listed["entries"]],
                    ["note.txt"],
                )
                self.assertEqual(listed["entries"][0]["type"], "file")
                patched = await workspace_tools.execute(
                    ToolCall(
                        "patch-1",
                        "apply_patch",
                        {
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
                structured = await workspace_tools.execute(
                    ToolCall(
                        "patch-structured",
                        "apply_patch",
                        {
                            "patch": (
                                "*** Begin Patch\n"
                                "*** Update File: note.txt\n"
                                "@@\n"
                                "-new\n"
                                "+structured\n"
                                "*** End Patch"
                            )
                        },
                    )
                )
                self.assertTrue(structured["ok"], structured)
                self.assertEqual(structured["format"], "structured")
                self.assertEqual(path.read_text(), "structured\n")
                wrapped = await workspace_tools.execute(
                    ToolCall(
                        "patch-wrapped",
                        "apply_patch",
                        {
                            "patch": (
                                "*** Begin Patch\n"
                                "diff --git a/note.txt b/note.txt\n"
                                "--- a/note.txt\n"
                                "+++ b/note.txt\n"
                                "@@ -1 +1 @@\n"
                                "-structured\n"
                                "+wrapped\n"
                                "*** End Patch"
                            )
                        },
                    )
                )
                self.assertTrue(wrapped["ok"], wrapped)
                self.assertEqual(path.read_text(), "wrapped\n")
                made = await workspace_tools.execute(
                    ToolCall("mkdir-1", "makedirs", {"path": "notes/archive"})
                )
                self.assertTrue(made["ok"], made)
                self.assertTrue(made["created"])
                self.assertTrue((Path(directory) / "notes/archive").is_dir())
                made_again = await workspace_tools.execute(
                    ToolCall("mkdir-2", "makedirs", {"path": "notes/archive"})
                )
                self.assertTrue(made_again["ok"], made_again)
                self.assertFalse(made_again["created"])
                moved = await workspace_tools.execute(
                    ToolCall(
                        "move-1",
                        "move_file",
                        {
                            "source": "note.txt",
                            "destination": "notes/archive/note.txt",
                        },
                    )
                )
                self.assertTrue(moved["ok"], moved)
                moved_path = Path(directory) / "notes/archive/note.txt"
                self.assertFalse(path.exists())
                self.assertEqual(moved_path.read_text(), "wrapped\n")
                deleted = await workspace_tools.execute(
                    ToolCall(
                        "delete-1",
                        "delete_file",
                        {"path": "notes/archive/note.txt"},
                    )
                )
                self.assertTrue(deleted["ok"], deleted)
                self.assertFalse(moved_path.exists())
            self.assertTrue(
                (await tools.execute(ToolCall("sleep-1", "sleep", {"seconds": 0})))[
                    "ok"
                ]
            )
        finally:
            await server.close()
