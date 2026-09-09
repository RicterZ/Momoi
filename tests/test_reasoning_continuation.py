import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.adapters.anthropic import merge_adjacent_roles
from momoi.integrations.adapters.openai import OpenAIProvider, openai_messages
from momoi.integrations.models import LLMConfig, ThinkingConfig
from momoi.models import IncomingMessage
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent.protocol import (
    assistant_history_message, handle_no_tool_response, owner_request_messages,
)
from tests.support import provider_catalog, recall_response


class ReasoningContinuationTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_loop_replays_reasoning_after_tools_and_protocol_corrections(self):
        requests = []
        violations = []
        # Include empty and whitespace-only values: replay must be lossless.
        thoughts = [" first thought\n", "", "  ", "fix arguments", "send reply", "finish"]
        recall = recall_response().tool_calls[0]
        finish = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}
        actions = [
            ("end_turn", json.dumps(finish)),  # Rejected: recall must be first.
            (recall.name, json.dumps(recall.arguments)),
            None,  # Text-only reply must also retain its reasoning during correction.
            ("send_bubbles", "{"),  # Invalid tool arguments return an error to the model.
            ("send_bubbles", json.dumps({"bubbles": ["修复后的回复"]})),
            ("end_turn", json.dumps(finish)),
        ]

        async def completion(request):
            payload = await request.json()
            index = len(requests)
            requests.append(payload)
            assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
            actual = [m.get("reasoning_content") for m in assistants]
            if actual != thoughts[:index]:
                violations.append((index, actual))
                return web.json_response({"error": {"message": "The reasoning_content in the thinking mode must be passed back to the API."}}, status=400)
            message = {"role": "assistant", "content": "Plain response" if actions[index] is None else None,
                       "reasoning_content": thoughts[index]}
            if actions[index] is not None:
                name, arguments = actions[index]
                message["tool_calls"] = [{"id": f"call-{index}", "type": "function",
                                          "function": {"name": name, "arguments": arguments}}]
            return web.json_response({"choices": [{"message": message}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completion)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                providers=provider_catalog(LLMConfig(
                    str(server.make_url("/v1")), "test", "deepseek-v4-flash", 1000, 0.6, 2, 0,
                    "openai", thinking=ThinkingConfig(effort="high"),
                )),
                channel=NapCatConfig("ws://127.0.0.1", "123", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.", transcript_turns_min=4, transcript_turns_max=4,
                episode_raw_tail_turns=2, memory_results=2, database=Path(directory)/"momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            try:
                async with daemon.services:
                    event = IncomingMessage("continuation-test", "continuation-test", "测试", 1, 1)
                    daemon.store.add_event(event)
                    await daemon._complete_batch_turn([event], asyncio.Event(), daemon._turn_id(event.event_id))
                self.assertEqual(violations, [])
                self.assertEqual(len(requests), len(actions))
                self.assertEqual([row.text for row in daemon.store.due_outbox()], ["修复后的回复"])
                self.assertIn("invalid_tool_arguments_json", json.dumps(requests[4]))
                self.assertNotIn("provider_continuation", json.dumps(requests))
            finally:
                daemon.store.close()

    async def test_provider_carries_reasoning_for_text_and_tool_responses(self):
        async def completion(request):
            return web.json_response({"choices": [{"message": {
                "content": "answer", "reasoning_content": "\nprivate thought\n",
            }}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completion)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        config = LLMConfig(str(server.make_url("/v1")), "test", "test", 100, 0, 2, 0)
        async with OpenAIProvider(config) as provider:
            response = await provider.complete("", [{"role": "user", "content": "question"}])
        self.assertNotIn("private thought", json.dumps(response.content))
        history = assistant_history_message(response.content, response.continuation)
        self.assertEqual(openai_messages("", [history])[0]["reasoning_content"], "\nprivate thought\n")
        history["provider_continuation"]["openai"]["reasoning_content"] = "changed"
        self.assertEqual(response.continuation["openai"]["reasoning_content"], "\nprivate thought\n")

    def test_all_text_correction_paths_preserve_provider_continuation(self):
        continuation = {"openai": {"reasoning_content": "private thought"}}
        for stage in ("workflow", "heartbeat", "goal", "owner", "webhook"):
            messages = []
            handle_no_tool_response(
                messages, [{"type": "text", "text": "answer"}],
                continuation=continuation, workflow_correction="retry" if stage == "workflow" else None,
                heartbeat_turn=stage == "heartbeat", harness_started=False,
                goal_turn=stage == "goal", require_response=True, owner_turn=stage == "owner",
                failed_rounds=0, last_tool_error="",
            )
            request = owner_request_messages(messages, remind_bubbles=True)
            self.assertEqual(openai_messages("", request)[0]["reasoning_content"], "private thought")
            self.assertEqual(messages[0]["content"], [{"type": "text", "text": "answer"}])

    def test_metadata_is_not_sent_to_other_protocol_or_added_to_unrelated_messages(self):
        messages = [assistant_history_message("answer", {"openai": {"reasoning_content": "thought"}})]
        before = copy.deepcopy(messages)
        self.assertEqual(merge_adjacent_roles(messages), [{"role": "assistant", "content": "answer"}])
        self.assertEqual(messages, before)
        self.assertEqual(openai_messages("", [{"role": "assistant", "content": "old answer"}]),
                         [{"role": "assistant", "content": "old answer"}])
