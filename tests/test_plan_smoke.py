"""Plan transcript contract and production owner-to-step integration tests.

Provider responses are scripted; execution and delivery use the real runtime.
The step() fixture isolates transcript edge cases; production tests exercise
public create/start tools, persistent claims and the production step workflow.
"""

import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent import AgentWorkflow, WorkflowProtocolError
from momoi.runtime.workflows.plan_context import plan_step_messages
from tests.support import provider_catalog


FINISH = {
    "name": "plan_step_finish", "description": "Report the current step outcome.",
    "input_schema": {
        "type": "object", "properties": {
            "outcome": {"enum": ["succeeded", "failed"]},
            "summary": {"type": "string", "minLength": 1},
        }, "required": ["outcome", "summary"], "additionalProperties": False,
    },
}


class PlanSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.daemon = MomoiDaemon(AppConfig(
            providers=provider_catalog(LLMConfig("http://localhost", "test", "model", 100, 0, 1, 0)),
            channel=NapCatConfig("ws://localhost", "123", 1, 60, 30, 30, 20),
            transcript_turns_min=4, transcript_turns_max=4, episode_raw_tail_turns=2,
            memory_results=2, log_level="INFO",
            system_prompt="Momoi role and voice", database=Path(directory.name) / "store.sqlite3",
        ))
        self.addCleanup(self.daemon.store.close)
        event = IncomingMessage("plan-request", "1", "看 A B C 的微博并发给我", 1, 1)
        self.daemon.store.add_event(event)
        self.owner_turn = self.daemon.store.commit_turn([event], event.text, AgentReply(["我来看看。 "]))
        self.turns = [self.owner_turn]
        self.records = []
        self.requests = []

    async def step(self, index, script):
        daemon = self.daemon
        turn_id = f"plan-smoke:{index}"
        daemon.store.begin_turn(turn_id, "plan_step", ["plan:smoke"])
        rows = daemon.store.conversation_messages_for_turns(self.turns) + self.records
        messages = plan_step_messages(
            rows, timezone=daemon.store.timezone,
            current_step=f"Original request: 看 A B C 的微博并发给我; step {index}",
            tool_activity=daemon.store.turn_activity(self.turns),
        )
        self.requests.append(copy.deepcopy(messages))
        remaining = iter(script)

        async def complete(*args, **kwargs):
            call = next(remaining)
            return ProviderResponse([{
                "type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments,
            }], [call])

        daemon.provider = SimpleNamespace(complete=complete)
        outcome = None

        async def finish(call):
            nonlocal outcome
            args = call.arguments
            if (set(args) != {"outcome", "summary"}
                    or args["outcome"] not in {"succeeded", "failed"}
                    or not isinstance(args["summary"], str) or not args["summary"].strip()):
                return {"ok": False, "error": "invalid_plan_step_outcome"}
            outcome = dict(args)
            return {"ok": True}

        result = await daemon._run_agent_workflow(
            daemon._system(), messages, [daemon.tool_surface.send_bubbles_spec(), FINISH],
            turn_id, AgentWorkflow(
                stage="plan_step", tool_names=frozenset({"plan_step_finish"}),
                execute_tool=finish, is_complete=lambda: outcome is not None,
                completion_result=lambda: outcome,
                no_tool_correction="Finish the current step with plan_step_finish.",
            ),
        )
        # Persistence adapter is deliberately test-only until Plan storage lands.
        with daemon.store._db:
            daemon.store._archive_progress_messages(turn_id, '["plan:smoke"]')
            daemon.store.complete_background_turn(turn_id)
        self.turns.append(turn_id)
        self.records.append({
            "id": 10000 + index, "turn_id": turn_id, "role": "plan_step",
            "content": f"Plan smoke; step {index}; {result['outcome']}: {result['summary']}",
            "created_at": time.time(), "delivery_state": "internal",
        })
        return result

    @staticmethod
    def finish(outcome="succeeded", summary="checked"):
        return ToolCall("finish", "plan_step_finish", {"outcome": outcome, "summary": summary})

    async def test_three_sends_advance_without_owner_continuation(self):
        await self.step(0, [self.finish(summary="access method confirmed")])
        for index, user in enumerate("ABC", 1):
            await self.step(index, [
                ToolCall("send", "send_bubbles", {"bubbles": [f"{user} latest post"]}),
                self.finish(summary=f"{user} queued"),
            ])
        pending = [row[0] for row in self.daemon.store._db.execute("SELECT text FROM outbox ORDER BY id")]
        self.assertEqual(pending[-3:], [f"{user} latest post" for user in "ABC"])
        last = self.requests[-1]
        text = str(last)
        self.assertEqual(text.count("A latest post"), 1)
        self.assertIn('delivery="queued"', text)
        self.assertIn('<plan_step id="P10000"', text)
        self.assertIn("access method confirmed", text)
        self.assertNotIn("owner did not reply", text)
        self.assertNotIn("previous assistant messages still being delivered", text)
        self.assertNotIn("ended the Turn without replying", text)
        self.assertNotIn("tool_use", text)
        self.assertIn("<current_plan_step>", str(last[-1]))
        self.assertNotIn("<current_plan_step>", str(last[:-1]))
        self.assertTrue(any(m["role"] == "assistant" and "A latest post" in str(m) for m in last))

    async def test_access_failure_stops_before_user_steps(self):
        for index in range(4):
            result = await self.step(index, [
                ToolCall("notice", "send_bubbles", {"bubbles": ["无法访问微博，任务已停止。"]}),
                self.finish("failed", "access unavailable"),
            ])
            if result["outcome"] == "failed":
                break
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.daemon.store.due_outbox()), 2)

    async def test_invalid_finish_uses_existing_circuit_breaker(self):
        with self.assertRaises(WorkflowProtocolError):
            await self.step(0, [
                ToolCall(str(i), "plan_step_finish", {})
                for i in range(self.daemon.config.turn_max_protocol_retries)
            ])
        self.assertEqual(self.records, [])

    def test_window_starting_with_assistant_keeps_native_speech(self):
        rows = [{"id": 1, "turn_id": "old", "role": "assistant", "content": "已写的原文",
                 "created_at": 1, "delivery_state": "uncertain"}]
        messages = plan_step_messages(rows, timezone=self.daemon.store.timezone, current_step="续写 <下一章>")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertIn("delivery uncertain", str(messages[1]))
        self.assertIn("&lt;下一章&gt;", str(messages[-1]))

    async def test_production_owner_create_start_and_all_steps(self):
        import asyncio
        import json
        from momoi.runtime.tool_contracts.context import RECALL_SKIP_EXAMPLE
        from momoi.runtime.transcript.building import build_transcript
        daemon = self.daemon
        event = IncomingMessage("production-plan-request", "1", "用 plan 分别发送甲乙丙", time.time(), 1)
        daemon.store.add_event(event)
        owner_id = daemon._turn_id(event.event_id)
        plan_id = None
        calls = 0
        start_request = {}
        seen = []

        async def complete(system, messages, tools, **kwargs):
            nonlocal calls, plan_id
            calls += 1
            seen.append(copy.deepcopy(messages))
            if calls == 1:
                call = ToolCall("recall", "recall", RECALL_SKIP_EXAMPLE)
            elif calls == 2:
                call = ToolCall("prelude", "send_bubbles", {"bubbles": ["分三步发给你。"]})
            elif calls == 3:
                call = ToolCall("create", "plan_create", {
                    "title": "发送三个结果", "request": event.text,
                    "steps": [{"task": f"发送{x}", "on_failure": "stop"} for x in "甲乙丙"],
                })
            elif calls == 4:
                start_request.update(system=copy.deepcopy(system), tools=copy.deepcopy(tools), messages=copy.deepcopy(messages))
                result = json.loads(messages[-1]["content"][0]["content"])
                plan_id = result["plan_id"]
                call = ToolCall("start", "plan_start", {"plan_id": plan_id})
            else:
                self.assertEqual(calls, 5)
                self.assertIsNone(daemon.store.claim_task_plan(), "must wait for owner commit")
                call = ToolCall("end", "end_turn", {"mood": {"decision": "unchanged"}, "reply_wait": {"wait": False}})
            return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])

        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_batch_turn([event], asyncio.Event(), owner_id, daemon.channel)
        self.assertEqual(daemon.store.task_plan(plan_id)["status"], "ready")
        from unittest.mock import patch
        fitter = patch.object(daemon.context_window, "fit", side_effect=AssertionError("step context must not be trimmed"))
        fitter.start()
        self.addCleanup(fitter.stop)
        frozen = copy.deepcopy(daemon.store.task_plan(plan_id)["context"])
        self.assertEqual(frozen, start_request)
        self.assertIn("plan_create", str(frozen["messages"]))
        self.assertNotIn('"name": "plan_start"', json.dumps(frozen["messages"]))
        for index, word in enumerate("甲乙丙"):
            self.assertIsNotNone(daemon.store.claim_task_plan())
            count = 0

            async def step_complete(system, messages, tools, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    self.assertEqual(system, frozen["system"])
                    self.assertEqual(tools, frozen["tools"])
                    self.assertEqual(messages[:len(frozen["messages"])], frozen["messages"])
                    self.assertEqual(len(messages), len(frozen["messages"]) + 2 * index + 1)
                    appended = messages[len(frozen["messages"]):-1]
                    self.assertEqual([m["role"] for m in appended], ["assistant", "user"] * index)
                    for previous, value in enumerate("甲乙丙"[:index]):
                        speech = appended[previous * 2]
                        record = appended[previous * 2 + 1]
                        self.assertIn("<bubble", str(speech))
                        self.assertIn(value, str(speech))
                        self.assertIn('<plan_step', str(record))
                        self.assertEqual(sum(block.get('text', '').count('>\n' + value + '\n</bubble>') for message in appended for block in message['content']), 1)
                    self.assertNotIn("tool_use", str(appended))
                    initial = str(messages)
                    self.assertIn('step_id="' + str(index + 1) + '"', initial)
                    self.assertIn("<request>" + event.text, initial)
                    self.assertIn("<task>发送" + word, initial)
                    self.assertNotIn("owner did not reply", initial)
                    self.assertNotIn("ended the Turn without replying", initial)
                    if index:
                        self.assertIn('<plan_step', initial)
                        self.assertIn('plan_id="' + plan_id, initial)
                        self.assertIn("<result>", initial)
                    self.assertIn("plan_create", [t["name"] for t in tools])
                    self.assertIn("end_turn", [t["name"] for t in tools])
                    call = ToolCall("send", "send_bubbles", {"bubbles": [word]})
                else:
                    self.assertEqual(count, 2)
                    call = ToolCall("finish", "plan_step_finish", {
                        "outcome": "succeeded", "summary": word + "已发送",
                        "output_refs": [], "abort_remaining": False,
                    })
                return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])

            daemon.provider = SimpleNamespace(complete=step_complete)
            await daemon._complete_plan_step_turn(plan_id, asyncio.Event())
            self.assertEqual(count, 2)
        self.assertEqual(daemon.store.task_plan(plan_id)["status"], "completed")
        self.assertIsNone(daemon.store.claim_task_plan())
        rows = daemon.store.recent_conversation_messages(20, 20000)
        transcript = str(build_transcript(rows, timezone=daemon.store.timezone).messages)
        self.assertIn("<plan_status>completed</plan_status>", transcript)
        self.assertEqual(transcript.count("<plan_step "), 3)
        sent = [r[0] for r in daemon.store._db.execute("SELECT text FROM outbox ORDER BY id")]
        self.assertEqual(sent[-3:], list("甲乙丙"))

    async def test_production_step_failure_stops_and_restart_does_not_replay(self):
        import asyncio
        daemon = self.daemon
        plan = daemon.store.create_task_plan({"title": "failure", "request": "test", "steps": [
            {"task": "access", "on_failure": "stop"}, {"task": "never", "on_failure": "stop"},
        ]}, self.owner_turn, daemon.channel.name)
        daemon.store.start_task_plan(plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        daemon.store.claim_task_plan()

        async def complete(*args, **kwargs):
            call = ToolCall("finish", "plan_step_finish", {
                "outcome": "failed", "summary": "cannot access", "output_refs": [], "abort_remaining": True,
            })
            return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])

        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan["id"], asyncio.Event())
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "failed")
        self.assertIsNone(daemon.store.claim_task_plan())
        with self.assertRaises(ValueError):
            daemon.store.start_task_plan(plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        other = daemon.store.create_task_plan({"title": "restart", "request": "test", "steps": [{"task": "x", "on_failure": "stop"}]}, self.owner_turn, daemon.channel.name)
        daemon.store.start_task_plan(other["id"], daemon.channel.name)
        daemon.store.claim_task_plan()
        daemon.store.recover_task_plans()
        self.assertEqual(daemon.store.task_plan(other["id"])["status"], "blocked")
        self.assertIsNone(daemon.store.claim_task_plan())

    async def test_production_successful_tools_cannot_loop_forever(self):
        import asyncio
        daemon = self.daemon
        plan = daemon.store.create_task_plan({"title": "bounded", "request": "test", "steps": [{"task": "read", "on_failure": "stop"}]}, self.owner_turn, daemon.channel.name)
        daemon.store.start_task_plan(plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        daemon.store.claim_task_plan()
        ref = daemon.tool_results.save('{"ok":true,"content":"unchanged"}')
        calls = 0

        async def complete(*args, **kwargs):
            nonlocal calls
            calls += 1
            self.assertLessEqual(calls, 24)
            call = ToolCall(str(calls), "read_tool_result", {"result_ref": ref})
            return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])

        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan["id"], asyncio.Event())
        self.assertEqual(calls, 24)
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "blocked")
        self.assertIsNone(daemon.store.claim_task_plan())

    async def test_stop_cancels_queued_plan_and_scheduler_claim_is_unique(self):
        daemon = self.daemon
        plan = daemon.store.create_task_plan({"title": "cancel", "request": "test", "steps": [{"task": "send", "on_failure": "stop"}]}, self.owner_turn, daemon.channel.name)
        daemon.store.start_task_plan(plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        daemon.store.start_task_plan(plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        self.assertEqual(daemon.store.claim_task_plan()["id"], plan["id"])
        self.assertIsNone(daemon.store.claim_task_plan())
        await daemon._receive(IncomingMessage("plan-stop", "1", "/stop", time.time(), 1))
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "cancelled")
        self.assertIsNone(daemon.store.claim_task_plan())

    def test_plan_tools_have_no_prelude_or_exclusive_batch_requirement(self):
        from momoi.runtime.agent import TurnHarness
        from momoi.runtime.tool_contracts.plan import PLAN_TOOLS
        from momoi.runtime.agent.progress import requires_owner_progress
        self.assertFalse(any(requires_owner_progress(tool) for tool in PLAN_TOOLS))
        harness = TurnHarness.for_stage("owner", progress_tool_names=self.daemon.tool_surface.owner_progress_tool_names())
        self.assertIsNone(harness.validate([
            ToolCall("recall", "recall", {}), ToolCall("create", "plan_create", {}),
            ToolCall("start", "plan_start", {}),
        ]))
        step = TurnHarness.for_stage("plan_step")
        self.assertIsNone(step.validate([
            ToolCall("send", "send_bubbles", {}), ToolCall("finish", "plan_step_finish", {}),
        ]))

    async def test_shared_schema_does_not_grant_step_owner_permissions(self):
        import asyncio
        import json
        daemon = self.daemon
        tools = daemon.tool_surface.conversation_specs()
        plan = daemon.store.create_task_plan({
            "title": "permissions", "request": "test",
            "steps": [{"task": "finish", "on_failure": "stop"}],
        }, self.owner_turn, daemon.channel.name)
        daemon.store.start_task_plan(plan["id"], daemon.channel.name, {
            "system": daemon._system(), "tools": tools, "messages": [],
        })
        daemon.store.claim_task_plan()
        calls = 0

        async def complete(system, messages, request_tools, **kwargs):
            nonlocal calls
            calls += 1
            self.assertEqual(request_tools, tools)
            if calls == 1:
                call = ToolCall("forbidden", "plan_create", {
                    "title": "must not exist", "request": "nested",
                    "steps": [{"task": "nested", "on_failure": "stop"}],
                })
            else:
                self.assertEqual(calls, 2)
                result = json.loads(messages[-1]["content"][0]["content"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "tool_not_allowed")
                call = ToolCall("finish", "plan_step_finish", {
                    "outcome": "succeeded", "summary": "done",
                    "output_refs": [], "abort_remaining": False,
                })
            return ProviderResponse([{
                "type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments,
            }], [call])

        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan["id"], asyncio.Event())
        self.assertEqual(calls, 2)
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "completed")
        self.assertEqual(daemon.store._db.execute("SELECT COUNT(*) FROM task_plans").fetchone()[0], 1)
        self.assertNotIn("plan_step_finish", daemon.tool_surface.permitted_names("owner"))
