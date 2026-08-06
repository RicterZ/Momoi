import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_planner import (
    ContextPlanError,
    degraded_context_plan,
    parse_context_plan,
)
from momoi.runtime.turns import CONTEXT_PLANNER_SYSTEM_PROMPT


def app_config(directory: str) -> AppConfig:
    return AppConfig(
        llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
        channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        system_prompt="test",
        recent_raw_tokens=1000,
        recent_turns=2,
        memory_results=2,
        memory_tokens=1000,
        database=Path(directory) / "momoi.sqlite3",
        log_level="INFO",
    )


def response_plan() -> dict[str, object]:
    return {
        "version": 1,
        "intent_units": [
            {
                "id": "social",
                "event_ids": ["event-1"],
                "text": "刷微博",
                "intent": "browse social feed",
                "speech_act": "casual_share",
                "references": [],
                "recall_queries": [],
            },
            {
                "id": "mail",
                "event_ids": ["event-1"],
                "text": "看邮件",
                "intent": "check mail",
                "speech_act": "request",
                "references": ["之前等的邮件"],
                "recall_queries": ["pending expected email thread"],
            },
        ],
        "episode_bindings": [
            {
                "episode_ref": "new:social",
                "title": "微博浏览",
                "relation": "primary",
                "unit_ids": ["social"],
                "topics": ["微博"],
                "entities": [],
                "open_loops": [],
                "salience": 0.4,
            },
            {
                "episode_ref": "new:mail",
                "title": "邮件跟进",
                "relation": "primary",
                "unit_ids": ["mail"],
                "topics": ["邮件"],
                "entities": [],
                "open_loops": ["等待中的邮件"],
                "salience": 0.8,
            },
        ],
        "episode_links": [
            {
                "from_episode_ref": "new:mail",
                "to_episode_ref": "new:social",
                "kind": "references",
            }
        ],
        "uncertainty": [],
    }


class ContextPlannerTest(unittest.TestCase):
    def test_parser_requires_event_coverage_and_normalizes_episode_refs(self) -> None:
        plan = response_plan()
        parsed = parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)
        bindings = parsed["episode_bindings"]
        self.assertEqual(len(bindings), 2)
        self.assertTrue(all(item["is_new"] for item in bindings))
        self.assertNotEqual(bindings[0]["episode_id"], bindings[1]["episode_id"])
        self.assertEqual(
            parsed["episode_links"][0]["from_episode_id"],
            bindings[1]["episode_id"],
        )

        plan["intent_units"][0]["event_ids"] = ["unknown"]
        with self.assertRaisesRegex(ContextPlanError, "unknown_event_id"):
            parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)

    def test_casual_units_can_skip_recall_and_do_not_create_open_loops(self) -> None:
        plan = response_plan()
        plan["episode_bindings"][0]["open_loops"] = ["饭后再弄"]
        parsed = parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)
        self.assertEqual(parsed["intent_units"][0]["recall_queries"], [])
        self.assertEqual(parsed["episode_bindings"][0]["open_loops"], [])

    def test_degraded_plan_splits_message_segments_and_marks_uncertainty(self) -> None:
        plan = degraded_context_plan(
            [
                {
                    "event_id": "event-1",
                    "channel": "napcat",
                    "text": "先查邮件；再看微博。",
                }
            ],
            "invalid_json",
        )
        self.assertEqual(len(plan["intent_units"]), 2)
        self.assertEqual(plan["episode_bindings"], [])
        self.assertIn("invalid_json", plan["uncertainty"][0])


class ContextPlannerAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_closed_episode_directory_allows_semantic_planner_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(app_config(directory))
            daemon.store.create_episode(
                "蓝色保温杯收纳",
                episode_id="old-cup",
                topics=["保温杯", "阁楼收纳"],
            )
            daemon.store._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', working_summary='杯子放在阁楼纸箱中'
                   WHERE id='old-cup'"""
            )
            daemon.store._db.commit()

            class Provider:
                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.assertEqual(system, CONTEXT_PLANNER_SYSTEM_PROMPT)
                    payload = json.loads(str(messages[0]["content"]))
                    self.assertIn(
                        "old-cup",
                        {item["id"] for item in payload["candidate_episodes"]},
                    )
                    plan = {
                        "version": 1,
                        "intent_units": [
                            {
                                "id": "u1",
                                "event_ids": ["semantic-event"],
                                "text": "我把喝水用的东西搁哪儿啦",
                                "intent": "find stored drinking container",
                                "references": ["喝水用的东西"],
                                "recall_queries": ["保温杯 阁楼 收纳位置"],
                            }
                        ],
                        "episode_bindings": [
                            {
                                "episode_ref": "old-cup",
                                "title": "蓝色保温杯收纳",
                                "relation": "primary",
                                "unit_ids": ["u1"],
                                "topics": ["保温杯", "阁楼收纳"],
                                "entities": [],
                                "open_loops": [],
                                "salience": 0.7,
                            }
                        ],
                        "episode_links": [],
                        "uncertainty": [],
                    }
                    return ProviderResponse(
                        [{"type": "text", "text": json.dumps(plan, ensure_ascii=False)}],
                        [],
                    )

            daemon.provider = Provider()  # type: ignore[assignment]
            event = IncomingMessage(
                "semantic-event",
                "semantic-event",
                "我把喝水用的东西搁哪儿啦",
                1,
                1,
            )
            turn_id = "semantic-turn"
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])

            planned = await daemon._plan_owner_context([event], turn_id)

            self.assertEqual(planned["episode_bindings"][0]["episode_id"], "old-cup")
            daemon.store.close()

    async def test_planner_runs_without_tools_before_main_and_commits_episodes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(app_config(directory))
            daemon.store.commit_turn(
                [],
                "GLOBAL RAW MUST NOT LEAK",
                AgentReply(["UNSELECTED ASSISTANT RAW"]),
                turn_id="unselected-turn",
            )
            for index in (1, 2):
                daemon.store.commit_turn(
                    [],
                    f"RECENT CONTEXT {index}",
                    AgentReply([f"RECENT REPLY {index}"]),
                    turn_id=f"recent-turn-{index}",
                )
            with daemon.store._db:
                daemon.store._db.executemany(
                    """INSERT INTO goals
                       (id, title, success_criteria, authority, source_event_id,
                        status, plan_json, next_action, next_review_at,
                        created_at, updated_at)
                       VALUES (?, ?, '完成', 'owner', 'source', 'active', '[]',
                               '继续', 9999999999, ?, ?)""",
                    [
                        (
                            f"goal-{index}",
                            "等待项目邮件" if index == 0 else f"无关任务 {index}",
                            index,
                            index,
                        )
                        for index in range(9)
                    ],
                )
                daemon.store._db.executemany(
                    """INSERT INTO reminders
                       (id, text, source_event_id, status, fire_at,
                        created_at, updated_at)
                       VALUES (?, ?, 'source', 'pending', 9999999999, ?, ?)""",
                    [
                        (
                            f"reminder-{index}",
                            "检查等待邮件" if index == 0 else f"无关提醒 {index}",
                            index,
                            index,
                        )
                        for index in range(9)
                    ],
                )

            class Provider:
                calls: list[str] = []
                planner_recent = ""
                main_rendered = ""

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    if system == CONTEXT_PLANNER_SYSTEM_PROMPT:
                        provider_self.calls.append("planner")
                        self.assertEqual(tools, [])
                        payload = json.loads(str(messages[0]["content"]))
                        self.assertEqual(
                            payload["owner_messages"][0]["text"],
                            "刷微博，也看下之前等的邮件",
                        )
                        recent = json.dumps(
                            payload["recent_conversation"], ensure_ascii=False
                        )
                        provider_self.planner_recent = recent
                        self.assertIn("RECENT CONTEXT 1", recent)
                        self.assertIn("RECENT CONTEXT 2", recent)
                        self.assertNotIn("GLOBAL RAW MUST NOT LEAK", recent)
                        self.assertEqual(len(payload["candidate_goals"]), 8)
                        self.assertEqual(
                            payload["candidate_goals"][0]["id"], "goal-0"
                        )
                        self.assertEqual(len(payload["candidate_reminders"]), 8)
                        self.assertEqual(
                            payload["candidate_reminders"][0]["id"], "reminder-0"
                        )
                        return ProviderResponse(
                            [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        response_plan(), ensure_ascii=False
                                    ),
                                }
                            ],
                            [],
                        )
                    provider_self.calls.append("main")
                    rendered = json.dumps(messages, ensure_ascii=False)
                    provider_self.main_rendered = rendered
                    self.assertIn("<context_resolution>", rendered)
                    self.assertIn('"speech_act":"casual_share"', rendered)
                    self.assertNotIn("<context_plan>", rendered)
                    self.assertNotIn("browse social feed", rendered)
                    self.assertNotIn("episode_bindings", rendered)
                    self.assertNotIn('"salience"', rendered)
                    self.assertIn("RECENT CONTEXT 2", rendered)
                    self.assertNotIn("GLOBAL RAW MUST NOT LEAK", rendered)
                    self.assertEqual(len(messages), 1)
                    call = ToolCall(
                        "respond",
                        "respond",
                        {
                            "messages": ["我分开看了这两件事。"],
                            "expects_reply": False,
                            "reply_expectation": "",
                            "mood": {"action": "keep"},
                        },
                    )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage("event-1", "1", "刷微博，也看下之前等的邮件", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)

            self.assertEqual(provider.calls, ["planner", "main"])
            self.assertIn("RECENT CONTEXT 1", provider.planner_recent)
            self.assertIn("RECENT CONTEXT 2", provider.main_rendered)
            self.assertNotIn("GLOBAL RAW MUST NOT LEAK", provider.main_rendered)
            stored = daemon.store.context_plan(turn_id)
            self.assertEqual(stored["state"], "recalled")
            self.assertEqual(stored["retrieval"]["version"], 2)
            self.assertEqual(len(stored["plan"]["intent_units"]), 2)
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT COUNT(*) FROM episode_turns WHERE turn_id=?", (turn_id,)
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT COUNT(*) FROM episode_links"
                ).fetchone()[0],
                1,
            )
            daemon.store.close()

    async def test_invalid_plans_retry_once_then_degrade_before_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(app_config(directory))

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    if system == CONTEXT_PLANNER_SYSTEM_PROMPT:
                        if provider_self.calls == 2:
                            self.assertIn("invalid_json", str(messages[-1]["content"]))
                        return ProviderResponse(
                            [{"type": "text", "text": "not json"}], []
                        )
                    rendered = json.dumps(messages, ensure_ascii=False)
                    self.assertNotIn("degraded_message_segment", rendered)
                    self.assertIn("<context_resolution>", rendered)
                    self.assertIn("may miss references", rendered)
                    call = ToolCall(
                        "respond",
                        "respond",
                        {
                            "messages": ["我先分别看邮件和微博。"],
                            "expects_reply": False,
                            "reply_expectation": "",
                            "mood": {"action": "keep"},
                        },
                    )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage("event-degraded", "1", "先查邮件；再看微博。", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)

            self.assertEqual(provider.calls, 3)
            stored = daemon.store.context_plan(turn_id)
            self.assertEqual(stored["state"], "degraded")
            self.assertEqual(len(stored["plan"]["intent_units"]), 2)
            fallback = daemon.store.list_episode_candidates()
            self.assertEqual(len(fallback), 1)
            self.assertEqual(
                daemon.store.episode_turns(str(fallback[0]["id"]))[0]["turn_id"],
                turn_id,
            )
            daemon.store.close()
