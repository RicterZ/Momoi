"""Plan workflow integration tests with scripted provider responses."""

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
from momoi.runtime.workflows.plan_context import frozen_plan_messages
from tests.support import provider_catalog


def approve_and_start(store, plan_id, channel, context=None):
    plan = store.task_plan(plan_id)
    if plan["status"] in {"draft", "awaiting_approval"}:
        if plan["status"] == "draft":
            plan = store.submit_task_plan(plan_id, channel, plan["version"], "方案", "已核实来源", "检查产物", "proposal-turn")
        return store.start_task_plan(plan_id, channel, context, version=plan["version"], approval={
            "event_id": "approval-event", "quote": "同意", "turn_id": "approval-turn", "received_at": time.time() + 1,
        })
    return store.start_task_plan(plan_id, channel, context)


class PlanSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_plan_does_not_repeat_original_owner_request_after_shared_history(self):
        plan = {"id": "p", "title": "Check", "request": "look at image",
                "step_index": 0, "steps": [{"id": "s", "task": "inspect",
                                              "on_failure": "stop", "status": "running"}]}
        source = {"role": "user", "content": [
            {"type": "text", "text": "<workflow_contract>owner only</workflow_contract><current_state>stale</current_state>"},
            {"type": "text", "text": "<current_owner_bubbles>look at image"},
            {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
            {"type": "text", "text": "</current_owner_bubbles>"},
            {"type": "text", "text": "<runtime_directives>owner permissions</runtime_directives>"},
        ]}
        messages = frozen_plan_messages(
            [{"role": "user", "content": "shared transcript"}], plan,
            step_rows=[], timezone=self.daemon.store.timezone,
            source_messages=[source],
        )
        self.assertEqual(messages[0]["content"], "shared transcript")
        self.assertEqual(len(messages), 2)
        self.assertNotIn("AAAA", str(messages))
        self.assertNotIn("owner only", str(messages))
        self.assertNotIn("stale", str(messages))
        self.assertNotIn("owner permissions", str(messages))
        self.assertIn("current_plan_step", str(messages[1]["content"]))

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.daemon = MomoiDaemon(AppConfig(
            providers=provider_catalog(LLMConfig("http://localhost", "test", "model", 100, 0, 1, 0)),
            channel=NapCatConfig("ws://localhost", "123", 1, 60, 30, 30, 20),
            transcript_turns_min=4, transcript_turns_max=4, episode_unsummarized_tail_turns=2,
            memory_results=2, log_level="INFO",
            system_prompt="Momoi role and voice", database=Path(directory.name) / "store.sqlite3",
        ))
        self.addCleanup(self.daemon.store.close)
        event = IncomingMessage("plan-request", "1", "看 A B C 的微博并发给我", 1, 1)
        self.daemon.store.add_event(event)
        self.owner_turn = self.daemon.store.commit_turn([event], event.text, AgentReply(["我来看看。 "]))

    async def test_three_sends_advance_without_owner_continuation(self):
        import asyncio

        daemon = self.daemon
        plan = daemon.store.create_task_plan({
            "title": "latest posts", "request": "send A B C", "steps": [
                {"task": f"send {user}", "on_failure": "stop"} for user in "ABC"
            ],
        }, self.owner_turn, daemon.channel.name)
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {
            "system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(),
            "messages": [],
        })
        requests = []
        for user in "ABC":
            self.assertEqual(daemon.store.claim_task_plan()["id"], plan["id"])
            calls = 0

            async def complete(system, messages, tools, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    requests.append(copy.deepcopy(messages))
                    call = ToolCall("send", "send_bubbles", {"bubbles": [f"{user} latest post"]})
                else:
                    call = ToolCall("finish", "plan_step_finish", {
                        "outcome": "succeeded", "summary": f"{user} queued",
                        "output_refs": [], "abort_remaining": False,
                    })
                return ProviderResponse([{
                    "type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments,
                }], [call])

            daemon.provider = SimpleNamespace(complete=complete)
            await daemon._complete_plan_step_turn(plan["id"], asyncio.Event())
            self.assertEqual(calls, 2)

        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "completed")
        self.assertIsNone(daemon.store.claim_task_plan())
        pending = [row[0] for row in daemon.store._db.execute("SELECT text FROM outbox ORDER BY id")]
        self.assertEqual(pending[-3:], [f"{user} latest post" for user in "ABC"])
        last = str(requests[-1])
        self.assertEqual(last.count("A latest post"), 1)
        self.assertIn("[message delivery confirmation] queued", last)
        self.assertIn("A queued", last)
        self.assertIn("B queued", last)
        self.assertIn("<current_plan_step", str(requests[-1][-1]))

    async def test_owner_message_pauses_running_plan_step(self):
        import asyncio

        daemon = self.daemon
        plan = daemon.store.create_task_plan(
            {"title": "old request", "request": "send old result", "steps": [
                {"task": "send old result", "on_failure": "stop"},
            ]}, self.owner_turn, daemon.channel.name,
        )
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {
            "system": daemon._system(),
            "tools": daemon.tool_surface.conversation_specs(),
            "messages": [],
        })
        daemon.store.claim_task_plan()
        started = asyncio.Event()

        async def complete(*args, **kwargs):
            started.set()
            await asyncio.sleep(3600)

        daemon.provider = SimpleNamespace(complete=complete)
        daemon._active_turn_stage = "plan_step"
        daemon._active_turn_channel = daemon.channel.name
        daemon._active_turn = asyncio.create_task(
            daemon._complete_plan_step_turn(plan["id"], asyncio.Event())
        )
        await asyncio.wait_for(started.wait(), 1)
        update = IncomingMessage("plan-update", "plan-update", "改一下要求", time.time(), time.time())
        await daemon._receive(update)
        with self.assertRaises(asyncio.CancelledError):
            await daemon._active_turn
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "paused")
        self.assertEqual(daemon.store.pending_events()[-1].text, update.text)
        self.assertEqual(daemon._interrupt_reason, "owner_update")
        self.assertIn("paused", daemon._interruption_notices[daemon.channel.name][0])

    def test_paused_plan_can_resume_or_update_remaining_steps(self):
        store = self.daemon.store
        plan = store.create_task_plan({
            "title": "three steps", "request": "send A B C", "steps": [
                {"task": f"send {item}", "on_failure": "stop"} for item in "ABC"
            ],
        }, self.owner_turn, self.daemon.channel.name)
        approve_and_start(store, plan["id"], self.daemon.channel.name, {
            "system": [], "tools": [], "messages": [],
        })
        store.claim_task_plan()
        interrupted = "plan-interrupted-safe"
        store.begin_turn(interrupted, "plan_step", [f"plan:{plan['id']}"])
        store.cancel_turn(interrupted, reason="owner_update")
        paused = store.pause_task_plan(plan["id"], interrupted)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(store.plan_resume_safety(paused), "safe")
        revised = store.update_task_plan(plan["id"], self.daemon.channel.name,
                                         paused["version"], [
            {"task": "send revised A", "on_failure": "stop"},
            {"task": "send B", "on_failure": "stop"},
        ], "send revised A and B")
        self.assertEqual(revised["request"], "send revised A and B")
        self.assertEqual(revised["steps"][0]["interrupted_turn_id"], interrupted)
        with self.assertRaises(ValueError):
            store.resume_task_plan(plan["id"], self.daemon.channel.name, revised["version"], {})
        resumed = approve_and_start(store, plan["id"], self.daemon.channel.name, {
            "system": [], "tools": [], "messages": [{"role": "user", "content": "BTW and correction"}],
        })
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["steps"][0]["task"], "send revised A")
        self.assertEqual(resumed["review"]["approved_version"], revised["version"])
        self.assertEqual(store.claim_task_plan()["id"], plan["id"])

    def test_paused_plan_with_visible_progress_cannot_replay(self):
        store = self.daemon.store
        plan = store.create_task_plan({
            "title": "sent something", "request": "send A", "steps": [
                {"task": "send A", "on_failure": "stop"},
            ],
        }, self.owner_turn, self.daemon.channel.name)
        approve_and_start(store, plan["id"], self.daemon.channel.name, {
            "system": [], "tools": [], "messages": [],
        })
        store.claim_task_plan()
        interrupted = "plan-interrupted-visible"
        store.begin_turn(interrupted, "plan_step", [f"plan:{plan['id']}"])
        store.queue_progress(interrupted, "sent-A", ["A"], self.daemon.channel.name)
        store.cancel_turn(interrupted, reason="owner_update")
        paused = store.pause_task_plan(plan["id"], interrupted)
        self.assertEqual(store.plan_resume_safety(paused), "requires_review")
        with self.assertRaisesRegex(ValueError, "may have acted externally"):
            store.resume_task_plan(plan["id"], self.daemon.channel.name,
                                   paused["version"], {"system": [], "tools": [], "messages": []})

    async def test_stop_cancels_active_webhook_turn(self):
        import asyncio

        daemon = self.daemon
        started = asyncio.Event()
        turn_id = "webhook:test-stop:0"

        async def complete_webhook(prompt, active_turn_id, channel):
            daemon.store.begin_turn(active_turn_id, "webhook", [active_turn_id])
            started.set()
            await asyncio.sleep(3600)

        daemon._complete_webhook_turn = complete_webhook
        stop = asyncio.Event()
        worker = asyncio.create_task(daemon._agent_worker(stop))
        request = asyncio.create_task(daemon._request_webhook_turn("old event", turn_id))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await daemon._receive(
                IncomingMessage("stop-webhook", "stop-webhook", "/stop", time.time(), time.time())
            )
            with self.assertRaisesRegex(RuntimeError, "owner_stop"):
                await asyncio.wait_for(request, 1)
            state = daemon.store._db.execute(
                "SELECT state FROM turns WHERE id=?", (turn_id,)
            ).fetchone()[0]
            self.assertEqual(state, "cancelled")
            self.assertFalse(daemon._webhook_turn_active)
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_production_step_failure_stops_and_restart_does_not_replay(self):
        import asyncio
        daemon = self.daemon
        plan = daemon.store.create_task_plan({"title": "failure", "request": "test", "steps": [
            {"task": "access", "on_failure": "stop"}, {"task": "never", "on_failure": "stop"},
        ]}, self.owner_turn, daemon.channel.name)
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
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
            approve_and_start(daemon.store, plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        other = daemon.store.create_task_plan({"title": "restart", "request": "test", "steps": [{"task": "x", "on_failure": "stop"}]}, self.owner_turn, daemon.channel.name)
        approve_and_start(daemon.store, other["id"], daemon.channel.name)
        daemon.store.claim_task_plan()
        daemon.store.recover_task_plans()
        self.assertEqual(daemon.store.task_plan(other["id"])["status"], "blocked")
        self.assertIsNone(daemon.store.claim_task_plan())

    async def test_production_successful_tools_cannot_loop_forever(self):
        import asyncio
        daemon = self.daemon
        plan = daemon.store.create_task_plan({"title": "bounded", "request": "test", "steps": [{"task": "read", "on_failure": "stop"}]}, self.owner_turn, daemon.channel.name)
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        daemon.store.claim_task_plan()
        ref = daemon.tool_results.save('{"ok":true,"content":"unchanged"}')
        calls = 0

        async def complete(*args, **kwargs):
            nonlocal calls
            calls += 1
            self.assertLessEqual(calls, 54)
            call = ToolCall(str(calls), "read_tool_result", {"result_ref": ref})
            return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])

        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan["id"], asyncio.Event())
        self.assertEqual(calls, 54)
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "paused")
        self.assertIsNone(daemon.store.claim_task_plan())

    async def test_stop_cancels_queued_plan_and_scheduler_claim_is_unique(self):
        daemon = self.daemon
        plan = daemon.store.create_task_plan({"title": "cancel", "request": "test", "steps": [{"task": "send", "on_failure": "stop"}]}, self.owner_turn, daemon.channel.name)
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {"system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(), "messages": []})
        self.assertEqual(daemon.store.claim_task_plan()["id"], plan["id"])
        self.assertIsNone(daemon.store.claim_task_plan())
        await daemon._receive(IncomingMessage("plan-stop", "1", "/stop", time.time(), 1))
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "cancelled")
        self.assertIsNone(daemon.store.claim_task_plan())

    def test_plan_tools_have_no_exclusive_batch_requirement(self):
        from momoi.runtime.agent import TurnHarness
        harness = TurnHarness.for_stage("owner")
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
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {
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
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "draft")
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
        self.assertEqual(daemon.store._db.execute("SELECT COUNT(*) FROM task_plans").fetchone()[0], 2)
        self.assertNotIn("plan_step_finish", daemon.tool_surface.permitted_names("owner"))


    async def test_plan_step_can_search_memory(self):
        import asyncio
        import json
        daemon = self.daemon
        plan = daemon.store.create_task_plan({
            "title": "memory search", "request": "look up memory",
            "steps": [{"task": "search memory", "on_failure": "stop"}],
        }, self.owner_turn, daemon.channel.name)
        approve_and_start(daemon.store, plan["id"], daemon.channel.name, {
            "system": daemon._system(), "tools": daemon.tool_surface.conversation_specs(),
            "messages": [],
        })
        daemon.store.claim_task_plan()
        calls = 0

        async def complete(system, messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                call = ToolCall("search", "memory_search", {"query": "游戏"})
            else:
                self.assertEqual(calls, 2)
                result = json.loads(messages[-1]["content"][0]["content"])
                self.assertTrue(result["ok"], result)
                call = ToolCall("finish", "plan_step_finish", {
                    "outcome": "succeeded", "summary": "searched",
                    "output_refs": [], "abort_remaining": False,
                })
            return ProviderResponse([{
                "type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments,
            }], [call])

        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan["id"], asyncio.Event())
        self.assertEqual(calls, 2)
        self.assertEqual(daemon.store.task_plan(plan["id"])["status"], "completed")

    def test_review_gate_rejects_early_and_stale_approval(self):
        store, channel = self.daemon.store, self.daemon.channel.name
        plan = store.create_task_plan({'title': 'review', 'request': 'inspect and change',
            'steps': [{'task': 'implement and verify', 'on_failure': 'stop'}]}, self.owner_turn, channel)
        with self.assertRaises(ValueError):
            store.start_task_plan(plan['id'], channel)
        self.assertIsNone(store.claim_task_plan())
        submitted = store.submit_task_plan(plan['id'], channel, 1, '方案', '依据', '验收', 'proposal')
        self.assertIsNone(store.claim_task_plan())
        for approval in [None, {'event_id': 'e', 'quote': '同意', 'turn_id': 'proposal', 'received_at': time.time()+1},
                         {'event_id': 'e', 'quote': '同意', 'turn_id': 'owner2', 'received_at': 0}]:
            with self.assertRaises(ValueError):
                store.start_task_plan(plan['id'], channel, {}, version=1, approval=approval)
        revised = store.update_task_plan(plan['id'], channel, 1,
            [{'task': 'different implementation', 'on_failure': 'stop'}])
        self.assertEqual(revised['status'], 'draft')
        self.assertIsNone(revised['review']['approved_version'])
        store.submit_task_plan(plan['id'], channel, 2, '修订', '新依据', '验收', 'proposal2')
        with self.assertRaises(ValueError):
            store.start_task_plan(plan['id'], channel, {}, version=1, approval={
                'event_id': 'e', 'quote': '同意', 'turn_id': 'owner2', 'received_at': time.time()+1})
        self.assertEqual(approve_and_start(store, plan['id'], channel, {})['status'], 'ready')
        self.assertEqual(store.claim_task_plan()['id'], plan['id'])

    def test_revision_preserves_completed_handoff_without_transcript(self):
        from momoi.runtime.workflows.plan_context import current_step_xml
        store, channel = self.daemon.store, self.daemon.channel.name
        plan = store.create_task_plan({'title': 'handoff', 'request': 'do work', 'steps': [
            {'task': 'inspect', 'on_failure': 'stop'}, {'task': 'implement', 'on_failure': 'stop'}]}, self.owner_turn, channel)
        approve_and_start(store, plan['id'], channel, {})
        store.claim_task_plan()
        store.begin_turn('step-one', 'plan_step', ['plan:'+plan['id']])
        store.finish_task_plan_step(plan['id'], 'step-one', 'succeeded', 'verified fact', ['tr_evidence'])
        revised = store.update_task_plan(plan['id'], channel, 1,
            [{'task': 'revised implementation', 'on_failure': 'stop'}])
        self.assertEqual(revised['step_index'], 1)
        self.assertEqual(revised['steps'][0]['result'], 'verified fact')
        self.assertEqual(revised['steps'][1]['task'], 'revised implementation')
        self.assertIsNone(store.claim_task_plan())
        rendered = current_step_xml(revised)
        self.assertIn('verified fact', rendered)
        self.assertIn('tr_evidence', rendered)

    async def test_running_plan_can_revise_and_submit_for_review(self):
        import asyncio
        import json
        daemon, channel = self.daemon, self.daemon.channel.name
        plan = daemon.store.create_task_plan({'title': 'revise', 'request': 'work',
            'steps': [{'task': 'investigate then implement', 'on_failure': 'stop'}]}, self.owner_turn, channel)
        approve_and_start(daemon.store, plan['id'], channel,
            {'tools': daemon.tool_surface.conversation_specs(), 'messages': []})
        daemon.store.claim_task_plan()
        count = 0
        async def complete(system, messages, tools, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                call = ToolCall('revise', 'plan_update', {'plan_id': plan['id'], 'version': 1,
                    'steps': [{'task': 'new approach with validation', 'on_failure': 'stop'}]})
            else:
                self.assertEqual(count, 2)
                self.assertEqual(json.loads(messages[-1]['content'][0]['content'])['status'], 'draft')
                call = ToolCall('submit', 'plan_submit', {'plan_id': plan['id'], 'version': 2,
                    'summary': '发现新事实。\n\n建议修改方案，请确认。', 'evidence': '工具证据', 'validation': '检查输出'})
            return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])
        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan['id'], asyncio.Event())
        current = daemon.store.task_plan(plan['id'])
        self.assertEqual(current['status'], 'awaiting_approval')
        self.assertEqual(current['version'], 2)
        self.assertIsNone(daemon.store.claim_task_plan())
        texts = [row[0] for row in daemon.store._db.execute('SELECT text FROM outbox')]
        self.assertEqual(texts.count('发现新事实。\n\n建议修改方案，请确认。'), 1)
        self.assertNotIn('发现新事实。', texts)
        self.assertNotIn('建议修改方案，请确认。', texts)
        self.assertEqual(current['review']['summary'], '发现新事实。\n\n建议修改方案，请确认。')

    async def test_resume_keeps_approval_version_but_uses_new_execution_turn(self):
        import asyncio
        daemon, channel = self.daemon, self.daemon.channel.name
        plan = daemon.store.create_task_plan({'title': 'resume', 'request': 'work',
            'steps': [{'task': 'inspect', 'on_failure': 'stop'}]}, self.owner_turn, channel)
        context = {'tools': daemon.tool_surface.conversation_specs(), 'messages': []}
        approve_and_start(daemon.store, plan['id'], channel, context)
        daemon.store.claim_task_plan()
        old_id = daemon._turn_id('plan_step', plan['id'], '1', 1, 0)
        daemon.store.begin_turn(old_id, 'plan_step', ['plan:'+plan['id']])
        daemon.store.cancel_turn(old_id, reason='owner_update')
        daemon.store.pause_task_plan(plan['id'], old_id)
        resumed = daemon.store.resume_task_plan(plan['id'], channel, 1, context)
        self.assertEqual(resumed['version'], 1)
        self.assertEqual(resumed['context']['resume_count'], 1)
        daemon.store.claim_task_plan()
        async def complete(*args, **kwargs):
            call = ToolCall('finish', 'plan_step_finish', {'outcome': 'succeeded', 'summary': 'verified',
                'output_refs': [], 'abort_remaining': False})
            return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])
        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan['id'], asyncio.Event())
        finished = daemon.store.task_plan(plan['id'])
        self.assertEqual(finished['status'], 'completed')
        self.assertNotEqual(finished['steps'][0]['turn_id'], old_id)

    async def test_failed_delivery_batch_cannot_report_success(self):
        import asyncio
        import json
        daemon, channel = self.daemon, self.daemon.channel.name
        plan = daemon.store.create_task_plan({'title': 'delivery', 'request': 'send result',
            'steps': [{'task': 'send verified result', 'on_failure': 'stop'}]}, self.owner_turn, channel)
        approve_and_start(daemon.store, plan['id'], channel,
            {'tools': daemon.tool_surface.conversation_specs(), 'messages': []})
        daemon.store.claim_task_plan()
        count = 0
        async def complete(system, messages, tools, **kwargs):
            nonlocal count
            count += 1
            outcome = {'outcome': 'succeeded', 'summary': 'sent', 'output_refs': [], 'abort_remaining': False}
            if count == 1:
                calls = [ToolCall('send', 'send_bubbles', {'bubbles': []}),
                         ToolCall('finish', 'plan_step_finish', outcome)]
            else:
                self.assertEqual(count, 2)
                result = json.loads(messages[-1]['content'][-1]['content'])
                self.assertEqual(result['error'], 'verify_failed_batch_before_finishing')
                calls = [ToolCall('failed', 'plan_step_finish', {**outcome, 'outcome': 'failed', 'summary': 'not delivered'})]
            return ProviderResponse([{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.arguments} for c in calls], calls)
        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan['id'], asyncio.Event())
        self.assertEqual(daemon.store.task_plan(plan['id'])['status'], 'failed')

    async def test_limit_is_reported_to_model_for_private_close(self):
        import asyncio
        from momoi.runtime.turn_support import TurnBudgetExceeded
        daemon = self.daemon
        plan = daemon.store.create_task_plan({'title': 'limit', 'request': 'work',
            'steps': [{'task': 'inspect', 'on_failure': 'stop'}]}, self.owner_turn, daemon.channel.name)
        approve_and_start(daemon.store, plan['id'], daemon.channel.name,
            {'tools': daemon.tool_surface.conversation_specs(), 'messages': []})
        daemon.store.claim_task_plan()
        before = daemon.store._db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        calls = 0
        async def loop(system, messages, tools, events, draft, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TurnBudgetExceeded('token limit reached')
            self.assertIn('已达到执行上限', str(messages[-1]))
            await kwargs['workflow'].execute_tool(ToolCall('close', 'plan_step_finish', {
                'outcome': 'blocked', 'summary': '缺少核实证据', 'output_refs': [], 'abort_remaining': True}))
        daemon._run_tool_loop = loop
        await daemon._complete_plan_step_turn(plan['id'], asyncio.Event())
        self.assertEqual(calls, 2)
        self.assertEqual(daemon.store.task_plan(plan['id'])['steps'][0]['result'], '缺少核实证据')
        self.assertEqual(daemon.store._db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], before)

    async def test_soft_audit_then_hard_pause_and_owner_resume(self):
        import asyncio
        import json
        daemon, channel = self.daemon, self.daemon.channel.name
        plan = daemon.store.create_task_plan({'title': 'audit', 'request': 'verify target',
            'steps': [{'task': 'find evidence', 'on_failure': 'stop'}]}, self.owner_turn, channel)
        approve_and_start(daemon.store, plan['id'], channel,
            {'tools': daemon.tool_surface.conversation_specs(), 'messages': []})
        daemon.store.claim_task_plan()
        ref = daemon.tool_results.save('{"ok":true,"content":"evidence"}')
        rounds = audits = closes = 0
        async def complete(system, messages, tools, **kwargs):
            nonlocal rounds, audits, closes
            if not tools:
                audits += 1
                self.assertEqual(rounds, 30)
                self.assertNotIn('Test soul', str(system))
                self.assertIn('独立的任务执行审计员', str(system))
                self.assertIn('verify target', str(messages))
                return ProviderResponse([{'type': 'text', 'text': '方向正确，继续核对证据'}], [])
            if '50次调用硬上限' in str(messages):
                closes += 1
                if closes == 1:
                    call = ToolCall('notify', 'send_bubbles', {'bubbles': ['已核对部分证据，关键来源仍不可用。继续查、调整方案，还是停止？']})
                else:
                    call = ToolCall('paused', 'plan_step_finish', {'outcome': 'blocked', 'summary': '关键来源不可用，保留既有证据', 'output_refs': [ref], 'abort_remaining': True})
            else:
                rounds += 1
                if rounds == 31:
                    self.assertIn('方向正确，继续核对证据', str(messages))
                call = ToolCall(str(rounds), 'read_tool_result', {'result_ref': ref})
            return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])
        daemon.provider = SimpleNamespace(complete=complete)
        await daemon._complete_plan_step_turn(plan['id'], asyncio.Event())
        paused = daemon.store.task_plan(plan['id'])
        self.assertEqual((rounds, audits, closes), (50, 1, 2))
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(paused['step_index'], 0)
        self.assertEqual(paused['steps'][0]['output_refs'], [ref])
        with self.assertRaises(ValueError):
            daemon.store.resume_task_plan(plan['id'], channel, 1, {})
        resumed = daemon.store.resume_task_plan(plan['id'], channel, 1, {}, owner_feedback={
            'event_id': 'new', 'quote': '继续', 'received_at': time.time()+1})
        self.assertEqual(resumed['status'], 'ready')
        self.assertEqual(resumed['step_index'], 0)
        self.assertNotIn('pause_reason', resumed['steps'][0])
        self.assertEqual(resumed['steps'][0]['owner_feedback']['quote'], '继续')
