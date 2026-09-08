import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.config.runtime_fields import THINKING_STAGES
from momoi.integrations.adapters.anthropic import AnthropicProvider
from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.models import LLMConfig, ThinkingConfig
from momoi.integrations.request_context import model_request, requested_thinking_effort
from momoi.models import ProviderResponse
from momoi.runtime.agent.harness import TURN_HARNESS_SPECS
from momoi.runtime.agent.model_round import ModelRoundRunner


class ModelRequestTest(unittest.IsolatedAsyncioTestCase):
    def runner(self, stages):
        return ModelRoundRunner(
            SimpleNamespace(fit=lambda *args: 0, check_budget=lambda *args: None),
            SimpleNamespace(record_turn_usage=Mock()),
            thinking_stages=stages,
        )

    async def run_round(self, runner, stage, complete):
        return await runner.run(
            [], [{"role": "user", "content": "Hello"}], [], complete=complete,
            system_policy=None, authority="agent", remind_owner_bubbles=False,
            harness_started=True, require_tool=False, required_tool=None,
            history_messages=0, stage=stage, turn_id="turn", round_number=1,
            channel="test", goal_id=None,
        )

    def test_all_runtime_stages_have_metadata(self):
        self.assertEqual(set(THINKING_STAGES), set(TURN_HARNESS_SPECS))

    async def test_runtime_stage_overrides_reach_both_provider_wire_formats(self):
        payloads = []

        async def handler(request):
            payload = await request.json()
            payloads.append(payload)
            if request.path == "/v1/messages":
                return web.json_response({"content": [{"type": "text", "text": "OK"}]})
            return web.json_response({"choices": [{"message": {"content": "OK"}}]})

        app = web.Application()
        app.router.add_post("/v1/messages", handler)
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        config = LLMConfig(
            base_url=str(server.make_url("/v1")), api_key="test", model="test",
            max_tokens=100, temperature=0.6, timeout_seconds=2, max_retries=0,
            thinking=ThinkingConfig(effort="high"),
        )
        stage_efforts = {
            "episode_anneal": "low", "reply_followup": "max",
            "reflection": "medium", "goal": "xhigh",
        }
        runner = self.runner(stage_efforts)
        for cls in (OpenAIProvider, AnthropicProvider):
            async with cls(config) as provider:
                async def complete(system, messages, tools, required, selected):
                    return await provider.complete(system, messages, tools, require_tool=required, required_tool=selected)

                for stage, expected in {**stage_efforts, "owner": "high"}.items():
                    await self.run_round(runner, stage, complete)
                    payload = payloads[-1]
                    actual = payload.get("reasoning_effort") if cls is OpenAIProvider else payload["output_config"]["effort"]
                    self.assertEqual(actual, expected)
                    self.assertNotIn("temperature", payload)
                self.assertEqual(provider.config.thinking.effort, "high")
                self.assertFalse(hasattr(provider.config.thinking, "stages"))
                await provider.complete("", [{"role": "user", "content": "Probe"}])
                last = payloads[-1]
                self.assertEqual(last.get("reasoning_effort", last.get("output_config", {}).get("effort")), "high")

    async def test_overrides_are_isolated_across_concurrent_requests_and_child_tasks(self):
        runner = self.runner({"episode_anneal": "low", "reply_followup": "max"})
        ready = asyncio.Event()
        arrived = 0
        observed = []

        async def complete(*_):
            nonlocal arrived
            initial = requested_thinking_effort("default")
            arrived += 1
            if arrived == 3:
                ready.set()
            await ready.wait()

            async def child():
                return requested_thinking_effort("default")

            observed.append((initial, await asyncio.create_task(child())))
            return ProviderResponse([], [])

        with model_request(thinking_effort="outer"):
            await asyncio.gather(*(self.run_round(runner, stage, complete) for stage in ("episode_anneal", "reply_followup", "owner")))
            self.assertEqual(requested_thinking_effort(), "outer")
        self.assertCountEqual(observed, [("low", "low"), ("max", "max"), ("default", "default")])
        self.assertEqual(requested_thinking_effort("default"), "default")

    async def test_failed_or_cancelled_request_resets_override(self):
        runner = self.runner({"episode_anneal": "low"})
        for error in (RuntimeError, asyncio.CancelledError):
            async def complete(*_):
                self.assertEqual(requested_thinking_effort(), "low")
                raise error()

            with self.assertRaises(error):
                await self.run_round(runner, "episode_anneal", complete)
            self.assertEqual(requested_thinking_effort("high"), "high")
