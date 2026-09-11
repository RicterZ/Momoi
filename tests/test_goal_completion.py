import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from inspect import isawaitable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import ProviderResponse, ToolCall, TurnDraft
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent import TurnHarness, TurnExecutionSpec
from momoi.runtime.transcript.building import build_transcript
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
    def test_shared_end_turn_schema_accepts_chat_or_empty_goal_completion(self):
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC

        check = Draft202012Validator(END_TURN_TOOL_SPEC["input_schema"])
        chat = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}
        self.assertTrue(check.is_valid(chat))
        self.assertTrue(check.is_valid({}))
        for args in ({"goal": {}}, {**chat, "goal": None}, {"mood": chat["mood"]}):
            self.assertFalse(check.is_valid(args))

    def test_goal_schema_requires_status_specific_outcomes(self):
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.conversation import GOAL_REVIEW_TOOL_SPEC

        validator = Draft202012Validator(GOAL_REVIEW_TOOL_SPEC["input_schema"])
        valid = [
            {"status": "done", "result": "Verified"},
            {"status": "cancelled", "result": "Owner stopped the task"},
            {
                "status": "active",
                "result": "Step 1 complete",
                "next_action": "Step 2",
                "next_review_at": future(),
            },
            {
                "status": "active",
                "result": "Periodic check complete",
                "next_action": "Check again",
            },
            {
                "status": "waiting",
                "result": "Submitted",
                "waiting_for": "Approval",
                "next_review_at": future(),
            },
            {
                "status": "blocked",
                "result": "Could not connect",
                "blocked_reason": "Missing credentials",
            },
        ]
        for outcome in valid:
            with self.subTest(outcome=outcome):
                self.assertTrue(validator.is_valid(outcome))
        for status in ("active", "waiting", "blocked"):
            with self.subTest(missing_fields=status):
                self.assertFalse(
                    validator.is_valid({"status": status, "result": "Checked"})
                )
        invalid = [
            {"status": "done", "result": "Verified", "next_action": "More work"},
            {"status": "cancelled", "result": "Stopped", "plan": []},
            {"status": "waiting", "result": "Submitted", "waiting_for": "Approval"},
            {
                "status": "blocked",
                "result": "Failed",
                "blocked_reason": "Credentials",
                "next_review_at": future(),
            },
            {"status": "active", "result": "Checked", "next_action": "  "},
            {"status": "done", "result": "  "},
        ]
        for outcome in invalid:
            with self.subTest(outcome=outcome):
                self.assertFalse(validator.is_valid(outcome))

    def test_goal_review_permission_and_completion_gate(self):
        review = ToolCall("review", "goal_review", {"status": "done", "result": "done"})
        end = ToolCall("end", "end_turn", {})
        for stage in ("owner", "heartbeat", "webhook", "reply_followup"):
            harness = TurnHarness.for_stage(
                stage, permitted_tool_names=frozenset({"goal_review"})
            )
            self.assertEqual(harness.validate([review]), "tool_not_allowed")
        harness = TurnHarness.for_stage("goal")
        self.assertEqual(
            harness.validate([end]), "goal_review_required_before_end_turn"
        )
        self.assertIsNone(harness.validate([review]))
        self.assertEqual(harness.validate([review, end]), "end_turn_must_be_alone")
        harness.accept("goal_review")
        self.assertIsNone(harness.validate([end]))
        for args in ({"goal": {}}, {"mood": {}}, {"reply_wait": {"wait": False}}):
            self.assertEqual(
                harness.validate([ToolCall("bad", "end_turn", args)]),
                "goal_end_turn_requires_empty_arguments",
            )
        harness.reset()
        self.assertEqual(
            harness.validate([end]), "goal_review_required_before_end_turn"
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

    async def test_all_outcomes_stage_then_commit_goal_turn(self):
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
                original = copy.deepcopy(self.daemon.store.goal(goal_id))

                def inspect(index, messages):
                    if index == 1:
                        self.assertEqual(
                            self.daemon.store.goal(goal_id)["status"],
                            original["status"],
                        )
                        self.assertEqual(
                            self.daemon.store.goal(goal_id)["latest_result"],
                            original["latest_result"],
                        )
                        self.assertIn('"staged"', str(messages[-1]))

                remaining, seen = self.provider(
                    [
                        ToolCall("end", "goal_review", outcome),
                        ToolCall("commit", "end_turn", {}),
                    ],
                    inspect=inspect,
                )
                await self.daemon._complete_goal_turn(goal_id, asyncio.Event())
                self.assertEqual(remaining, [])
                self.assertEqual(len(seen), 2)
                goal = self.daemon.store.goal(goal_id)
                self.assertEqual(goal["status"], outcome["status"])
                self.assertEqual(goal["latest_result"], outcome["result"])
                historical = self.daemon.store.recent_conversation_messages(1, 10000)
                self.assertEqual([item["role"] for item in historical], ["goal"])
                transcript = build_transcript(
                    historical, timezone=self.daemon.store.timezone
                )
                self.assertIn(f"Status: {outcome['status']}", str(transcript.messages))
                self.assertIn(outcome["result"], str(transcript.messages))
                self.assertIn('<goal id="G', str(transcript.messages))
                self.assertNotIn("<bubble>", str(transcript.messages))
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

    async def test_next_goal_reads_immutable_review_result_without_a_message(self):
        store = self.daemon.store
        initial_window = store.transcript_window_turn_limit(4, 8)
        self.provider(
            [
                ToolCall(
                    "finish-first",
                    "goal_review",
                    {
                        "status": "done",
                        "result": "Checked: no notification needed",
                    },
                ),
                ToolCall("commit", "end_turn", {}),
            ]
        )
        await self.daemon._complete_goal_turn(self.goal_id, asyncio.Event())
        original = store.recent_conversation_messages(10, 10000)
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0]["role"], "goal")
        stable_id = f"G{original[0]['id']}"
        self.assertEqual(store.due_outbox(), [])
        # Editing the live Goal cannot rewrite an earlier review's snapshot.
        with store._db:
            store._db.execute(
                "UPDATE goals SET latest_result='New live result' WHERE id=?",
                (self.goal_id,),
            )
        other_goal = self.create_goal()

        def inspect(_round, messages):
            if _round:
                return
            history = str(messages)
            self.assertIn(f'<goal id="{stable_id}"', history)
            self.assertIn("Checked: no notification needed", history)
            self.assertNotIn("New live result", history)
            self.assertNotIn("without replying", history)
            current = messages[-1]["content"][0]["text"]
            self.assertIn(f"<recent_goals>\n{stable_id}\n</recent_goals>", current)
            self.assertNotIn("Checked: no notification needed", current)
            self.assertIn(f'<goal id="{other_goal}"', current)
            for tag in ("episode_directory", "recall_memories", "reflection_memories"):
                self.assertNotIn(f"<{tag}>", current)

        self.provider(
            [
                ToolCall(
                    "finish-next",
                    "goal_review",
                    {
                        "status": "done",
                        "result": "Second review complete",
                    },
                ),
                ToolCall("commit", "end_turn", {}),
            ],
            inspect=inspect,
        )
        with patch.object(
            store,
            "ranked_memory_context",
            side_effect=AssertionError("unexpected pre-retrieval"),
        ):
            await self.daemon._complete_goal_turn(other_goal, asyncio.Event())
        self.assertEqual(store.transcript_window_turn_limit(4, 8), initial_window + 1)
        reviews = store.recent_conversation_messages(10, 10000)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]["content"], original[0]["content"])
        self.assertEqual(
            self.daemon.store._db.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0],
            0,
        )

    async def test_recent_goals_indexes_every_retained_review_in_order(self):
        store = self.daemon.store
        for index in range(6):
            turn_id = f"review-{index}"
            store.begin_turn(turn_id, "goal", [f"goal:{self.goal_id}"])
            store.commit_autonomous_turn(self.goal_id, TurnDraft(), turn_id=turn_id)
        retained = store.recent_conversation_messages(4, 10000)
        expected = [f"G{row['id']}" for row in retained if row["role"] == "goal"]
        self.assertEqual(len(expected), 4)

        def inspect(_round, messages):
            if _round:
                return
            current = messages[-1]["content"][0]["text"]
            self.assertIn(
                "<recent_goals>\n" + ", ".join(expected) + "\n</recent_goals>", current
            )
            history = str(messages[:-1])
            for identifier in expected:
                self.assertEqual(history.count(f'<goal id="{identifier}"'), 1)

        self.provider(
            [
                ToolCall(
                    "finish",
                    "goal_review",
                    {
                        "status": "done",
                        "result": "Complete",
                    },
                ),
                ToolCall("commit", "end_turn", {}),
            ],
            inspect=inspect,
        )
        await self.daemon._complete_goal_turn(self.goal_id, asyncio.Event())

    async def test_recurring_goal_keeps_its_schedule_without_extra_update(self):
        goal_id = self.create_goal({"kind": "interval", "every_seconds": 3600})
        self.provider(
            [
                ToolCall(
                    "end",
                    "goal_review",
                    {
                        "status": "active",
                        "result": "Checked",
                        "next_action": "Check again",
                    },
                ),
                ToolCall("commit", "end_turn", {}),
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
                self.assertEqual(
                    self.daemon.store.goal(self.goal_id)["status"], "active"
                )
                self.assertEqual(
                    self.daemon.store._db.execute(
                        "SELECT COUNT(*) FROM notifications"
                    ).fetchone()[0],
                    0,
                )
            if index == 1:
                result = json.loads(messages[-1]["content"][0]["content"])
                self.assertEqual(result["state"], "committed")
                self.assertEqual(result["provenance"]["source"], "runtime")
                self.assertEqual(self.daemon.store.due_outbox()[0].text, "文件已验证")
                stop = asyncio.Event()
                self.daemon.channel.send_message = AsyncMock(
                    side_effect=lambda *_: stop.set()
                )
                await asyncio.wait_for(self.daemon._outbox_worker(stop), 1)
                self.daemon.channel.send_message.assert_awaited_once()
                self.assertEqual(self.daemon.store.due_outbox(), [])
            if index == 2:
                error = json.loads(messages[-1]["content"][0]["content"])
                self.assertFalse(error["ok"])
                self.assertEqual(error["error"], "invalid_goal_outcome")
            if index == 3:
                self.assertEqual(
                    self.daemon.store.due_outbox()[0].text, "下载目录也清理完了"
                )

        remaining, seen = self.provider(
            [
                ToolCall("notice", "send_bubbles", {"bubbles": ["文件已验证"]}),
                ToolCall(
                    "invalid", "goal_review", {"status": "waiting", "result": "waiting"}
                ),
                ToolCall("more", "send_bubbles", {"bubbles": ["下载目录也清理完了"]}),
                ToolCall(
                    "end", "goal_review", {"status": "done", "result": "File validated"}
                ),
                ToolCall("commit", "end_turn", {}),
            ],
            inspect,
        )
        await self.daemon._complete_goal_turn(self.goal_id, asyncio.Event())
        self.assertEqual(remaining, [])
        self.assertEqual(len(seen), 5)
        self.assertEqual(self.daemon.store.goal(self.goal_id)["status"], "done")
        rows = self.daemon.store._db.execute(
            "SELECT content, delivery_state FROM messages WHERE outbox_id IS NOT NULL ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("文件已验证", "delivered"),
                ("下载目录也清理完了", "queued"),
            ],
        )
        self.assertEqual(
            self.daemon.store._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            2,
        )

    async def test_message_is_not_lost_when_goal_fails_before_finish(self):
        def inspect(index, messages):
            if index == 1:
                raise RuntimeError("provider unavailable")

        self.provider(
            [ToolCall("notice", "send_bubbles", {"bubbles": ["已完成第一步"]})], inspect
        )
        await self.daemon._complete_goal_turn(self.goal_id, asyncio.Event())
        self.assertEqual(self.daemon.store.goal(self.goal_id)["status"], "active")
        self.assertEqual(
            [row.text for row in self.daemon.store.due_outbox()], ["已完成第一步"]
        )

    async def test_staged_goal_review_is_discarded_on_cancel_or_provider_failure(self):
        for cancelled in (False, True):
            goal_id = self.create_goal()
            original = copy.deepcopy(self.daemon.store.goal(goal_id))

            def inspect(index, _messages):
                if index == 1:
                    self.assertEqual(
                        self.daemon.store.goal(goal_id)["status"], "active"
                    )
                    if cancelled:
                        raise asyncio.CancelledError
                    raise RuntimeError("provider unavailable before end_turn")

            self.provider(
                [
                    ToolCall(
                        "review",
                        "goal_review",
                        {"status": "done", "result": "not committed"},
                    )
                ],
                inspect,
            )
            if cancelled:
                with self.assertRaises(asyncio.CancelledError):
                    await self.daemon._complete_goal_turn(goal_id, asyncio.Event())
            else:
                await self.daemon._complete_goal_turn(goal_id, asyncio.Event())
            actual = self.daemon.store.goal(goal_id)
            self.assertEqual(actual["status"], original["status"])
            self.assertNotEqual(actual["latest_result"], "not committed")

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
                self.assertIn("unexpected_end_turn_fields", str(messages[-1]))
                self.assertEqual(self.daemon.store.goal(self.goal_id), original)

        remaining, seen = self.provider(
            [
                ToolCall(
                    "wrong",
                    "end_turn",
                    {**ordinary, "goal": {"status": "done", "result": "wrong"}},
                ),
                ToolCall("correct", "end_turn", ordinary),
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
        permitted = self.daemon.tool_surface.permitted_names("goal")
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
