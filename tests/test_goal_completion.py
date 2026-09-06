import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from inspect import isawaitable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import ProviderResponse, ToolCall, TurnDraft
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent import TurnHarness, TurnExecutionSpec
from tests.support import provider_catalog


def future():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def response(call):
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


class GoalBoundaryTest(unittest.TestCase):
    def test_shared_schema_accepts_chat_or_goal_without_mixing(self):
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC

        schema = END_TURN_TOOL_SPEC["input_schema"]
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        chat = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}
        goal = {"status": "done", "result": "Verified"}
        for args in (chat, {**chat, "goal": None}, {"goal": goal}):
            self.assertTrue(validator.is_valid(args))
        for args in (
            {},
            {"goal": None},
            {**chat, "goal": goal},
            {"goal": {**goal, "goal_id": "other"}},
            {"goal": {"status": "done"}},
        ):
            self.assertFalse(validator.is_valid(args))

    def test_goal_argument_is_restricted_by_trusted_turn_stage(self):
        for stage in ("owner", "heartbeat", "webhook", "reply_followup"):
            harness = TurnHarness.for_stage(stage)
            harness.started = True
            for goal in ({"status": "done", "result": "done"}, {}, "", False, []):
                with self.subTest(stage=stage, goal=goal):
                    self.assertEqual(
                        harness.validate([ToolCall("end", "end_turn", {"goal": goal})]),
                        "goal_not_allowed_in_end_turn",
                    )
            for args in ({}, {"goal": None}):
                self.assertIsNone(harness.validate([ToolCall("end", "end_turn", args)]))
        harness = TurnHarness.for_stage("goal")
        for args in ({}, {"goal": None}, {"goal": []}, {"goal": False}):
            self.assertEqual(
                harness.validate([ToolCall("end", "end_turn", args)]),
                "goal_required_in_end_turn",
            )
        goal = {"status": "done", "result": "done"}
        self.assertIsNone(
            harness.validate([ToolCall("end", "end_turn", {"goal": goal})])
        )
        for name in ("reply_wait", "mood", "activity", "heartbeat", "goal_id"):
            self.assertEqual(
                harness.validate(
                    [ToolCall("end", "end_turn", {"goal": goal, name: None})]
                ),
                "goal_end_turn_only_accepts_goal",
            )
        self.assertEqual(
            harness.validate(
                [
                    ToolCall("end", "end_turn", {"goal": goal}),
                    ToolCall("send", "send_bubbles", {}),
                ]
            ),
            "end_turn_must_be_alone",
        )


class GoalCompletionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.daemon = MomoiDaemon(
            AppConfig(
                providers=provider_catalog(
                    LLMConfig("http://localhost", "test", "model", 100, 0, 1, 0)
                ),
                channel=NapCatConfig("ws://localhost", "123", 1, 60, 30, 30, 20),
                system_prompt="test",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory.name) / "store.sqlite3",
                log_level="INFO",
            )
        )
        self.addCleanup(self.daemon.store.close)
        self.goal_id = self.create_goal()

    def create_goal(self, schedule=None):
        draft = TurnDraft()
        args = {
            "title": "Check file",
            "success_criteria": "File validated",
            "next_action": "Check file",
        }
        args.update(
            {"schedule": schedule} if schedule else {"next_review_at": future()}
        )
        result = self.daemon.agenda_tools.execute(
            ToolCall("create", "goal_create", args),
            draft,
            authority="owner",
            source_event_id="test",
        )
        self.assertTrue(result["ok"])
        self.daemon.store.commit_goal_draft(draft)
        return result["goal"]["id"]

    def provider(self, calls, inspect=None):
        remaining = list(calls)
        seen = []

        async def complete(system, messages, tools, **kwargs):
            if inspect:
                inspected = inspect(len(seen), messages)
                if isawaitable(inspected):
                    await inspected
            call = remaining.pop(0)
            seen.append(call)
            return response(call)

        self.daemon.provider = SimpleNamespace(
            config=SimpleNamespace(api_format="anthropic"), complete=complete
        )
        return remaining, seen

    async def test_all_outcomes_end_goal_turn_with_one_call(self):
        outcomes = [
            {"status": "done", "result": "File validated"},
            {"status": "cancelled", "result": "Superseded by another task"},
            {
                "status": "active",
                "result": "First check complete",
                "next_action": "Check remaining file",
                "next_review_at": future(),
            },
            {
                "status": "waiting",
                "result": "Submitted",
                "waiting_for": "Approval",
                "next_review_at": future(),
            },
            {
                "status": "blocked",
                "result": "Could not authenticate",
                "blocked_reason": "Need a credential",
            },
        ]
        for outcome in outcomes:
            with self.subTest(status=outcome["status"]):
                goal_id = self.create_goal()
                remaining, seen = self.provider(
                    [ToolCall("end", "end_turn", {"goal": outcome})]
                )
                await self.daemon._complete_goal_turn(goal_id, asyncio.Event())
                self.assertEqual(remaining, [])
                self.assertEqual(len(seen), 1)
                goal = self.daemon.store.goal(goal_id)
                self.assertEqual(goal["status"], outcome["status"])
                self.assertEqual(goal["latest_result"], outcome["result"])
                if outcome["status"] in {"done", "cancelled", "blocked"}:
                    self.assertIsNone(goal["next_review_at"])
                else:
                    self.assertGreater(
                        goal["next_review_at"], datetime.now(timezone.utc).timestamp()
                    )
                self.assertIsNone(goal["review_claimed_at"])
                row = self.daemon.store._db.execute(
                    "SELECT state FROM turns WHERE workflow_kind=? ORDER BY rowid DESC LIMIT 1",
                    ("goal",),
                ).fetchone()
                self.assertEqual(row["state"], "completed")
        self.assertEqual(
            self.daemon.store._db.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0],
            0,
        )

    async def test_recurring_goal_keeps_its_schedule_without_extra_update(self):
        goal_id = self.create_goal({"kind": "interval", "every_seconds": 3600})
        self.provider(
            [
                ToolCall(
                    "end",
                    "end_turn",
                    {
                        "goal": {
                            "status": "active",
                            "result": "Checked",
                            "next_action": "Check again",
                        }
                    },
                )
            ]
        )
        await self.daemon._complete_goal_turn(goal_id, asyncio.Event())
        goal = self.daemon.store.goal(goal_id)
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["schedule"], {"kind": "interval", "every_seconds": 3600})
        self.assertGreater(
            goal["next_review_at"], datetime.now(timezone.utc).timestamp() + 3500
        )

    async def test_messages_deliver_before_finish_and_survive_invalid_outcome(self):
        async def inspect(index, messages):
            if index in (1, 2, 3):
                self.assertEqual(self.daemon.store.goal(self.goal_id)["status"], "active")
                self.assertEqual(
                    self.daemon.store._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0
                )
            if index == 1:
                result = json.loads(messages[-1]["content"][0]["content"])
                self.assertEqual(result["state"], "committed")
                self.assertEqual(result["provenance"]["source"], "runtime")
                self.assertEqual(self.daemon.store.due_outbox()[0].text, "文件已验证")
                stop = asyncio.Event()
                self.daemon.channel.send_message = AsyncMock(side_effect=lambda *_: stop.set())
                await asyncio.wait_for(self.daemon._outbox_worker(stop), 1)
                self.daemon.channel.send_message.assert_awaited_once()
                self.assertEqual(self.daemon.store.due_outbox(), [])
            if index == 2:
                error = json.loads(messages[-1]["content"][0]["content"])
                self.assertFalse(error["ok"])
                self.assertEqual(error["error"], "invalid_goal_outcome")
            if index == 3:
                self.assertEqual(self.daemon.store.due_outbox()[0].text, "下载目录也清理完了")

        remaining, seen = self.provider(
            [
                ToolCall("notice", "send_bubbles", {"bubbles": ["文件已验证"]}),
                ToolCall("invalid", "end_turn", {"goal": {"status": "waiting", "result": "waiting"}}),
                ToolCall("more", "send_bubbles", {"bubbles": ["下载目录也清理完了"]}),
                ToolCall("end", "end_turn", {"goal": {"status": "done", "result": "File validated"}}),
            ], inspect,
        )
        await self.daemon._complete_goal_turn(self.goal_id, asyncio.Event())
        self.assertEqual(remaining, [])
        self.assertEqual(len(seen), 4)
        self.assertEqual(self.daemon.store.goal(self.goal_id)["status"], "done")
        rows = self.daemon.store._db.execute(
            "SELECT content, delivery_state FROM messages WHERE outbox_id IS NOT NULL ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("文件已验证", "delivered"), ("下载目录也清理完了", "queued"),
        ])
        self.assertEqual(self.daemon.store._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)

    async def test_message_is_not_lost_when_goal_fails_before_finish(self):
        def inspect(index, messages):
            if index == 1:
                raise RuntimeError("provider unavailable")

        self.provider([ToolCall("notice", "send_bubbles", {"bubbles": ["已完成第一步"]})], inspect)
        await self.daemon._complete_goal_turn(self.goal_id, asyncio.Event())
        self.assertEqual(self.daemon.store.goal(self.goal_id)["status"], "active")
        self.assertEqual([row.text for row in self.daemon.store.due_outbox()], ["已完成第一步"])

    async def test_goal_outcome_validation_never_mutates_draft_on_failure(self):
        invalid = [
            {"status": "done", "result": ""},
            {"status": "invented", "result": "result"},
            {"status": "done", "result": "ok", "goal_id": "another-goal"},
            {"status": "done", "result": "ok", "next_review_at": future()},
            {
                "status": "active",
                "result": "ok",
                "next_action": "continue",
                "next_review_at": "2000-01-01T00:00:00+00:00",
            },
            {"status": "waiting", "result": "ok", "waiting_for": "approval"},
            {"status": "blocked", "result": "ok"},
            {
                "status": "active",
                "result": "ok",
                "next_action": "continue",
                "plan": "bad",
            },
        ]
        draft = TurnDraft()
        original = copy.deepcopy(self.daemon.store.goal(self.goal_id))
        for decision in invalid:
            with self.subTest(decision=decision):
                result = self.daemon.agenda_tools.finish_review(
                    self.goal_id, decision, draft
                )
                self.assertFalse(result["ok"])
                self.assertEqual(draft.goals, {})
                self.assertEqual(self.daemon.store.goal(self.goal_id), original)

    async def test_webhook_cannot_update_goal_through_end_turn(self):
        original = copy.deepcopy(self.daemon.store.goal(self.goal_id))
        self.daemon.agenda_tools.finish_review = AsyncMock(
            side_effect=AssertionError("must not execute")
        )
        ordinary = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}

        def inspect(index, messages):
            if index == 1:
                self.assertIn("goal_not_allowed_in_end_turn", str(messages[-1]))
                self.assertEqual(self.daemon.store.goal(self.goal_id), original)

        remaining, seen = self.provider(
            [
                ToolCall(
                    "wrong",
                    "end_turn",
                    {**ordinary, "goal": {"status": "done", "result": "wrong"}},
                ),
                ToolCall("correct", "end_turn", {**ordinary, "goal": None}),
            ],
            inspect,
        )
        self.daemon.store.begin_turn("webhook-test", "webhook", [])
        await self.daemon._run_tool_loop(
            [],
            [{"role": "user", "content": "test"}],
            self.daemon.tool_surface.conversation_specs(),
            [],
            TurnDraft(),
            execution=TurnExecutionSpec(
                "webhook",
                permitted_tools=self.daemon.tool_surface.permitted_names("webhook"),
            ),
            source_event_id="test",
            turn_id="webhook-test",
            delivery_channel=self.daemon.channel,
        )
        self.assertEqual(remaining, [])
        self.assertEqual(len(seen), 2)
        self.daemon.agenda_tools.finish_review.assert_not_called()

    def test_goal_surface_restricts_mutations_to_terminal_payload(self):
        surface = {
            tool["name"] for tool in self.daemon.tool_surface.conversation_specs()
        }
        self.assertIn("end_turn", surface)
        for owned in (True, False):
            permitted = self.daemon.tool_surface.permitted_names(
                "goal", agent_owned_goal=owned
            )
            harness = TurnHarness.for_stage("goal", permitted_tool_names=permitted)
            for name in ("goal_update", "goal_finish", "goal_cancel"):
                self.assertEqual(
                    harness.validate(
                        [ToolCall("mutation", name, {"goal_id": self.goal_id})]
                    ),
                    "tool_not_allowed",
                )
        self.assertTrue(
            {"goal_update", "goal_finish", "goal_cancel"}
            <= self.daemon.tool_surface.permitted_names("owner")
        )
