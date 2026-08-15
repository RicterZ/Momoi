import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_assembler import build_plan_retrieval
from momoi.runtime.context_planner import (
    CONTEXT_PLAN_TOOL_NAME,
    CONTEXT_PLAN_TOOL_SPEC,
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


def tool_plan_response(plan: dict[str, object]) -> ProviderResponse:
    call = ToolCall("context-plan", CONTEXT_PLAN_TOOL_NAME, plan)
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


class ContextPlannerTest(unittest.TestCase):
    def test_context_plan_shape_lives_in_tool_schema(self) -> None:
        self.assertIn(CONTEXT_PLAN_TOOL_NAME, CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn('"intent_units"', CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("structured return", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn("converse, call tools", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn("1-12", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn("0-6", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn("allowed kinds", CONTEXT_PLANNER_SYSTEM_PROMPT)
        schema = CONTEXT_PLAN_TOOL_SPEC["input_schema"]
        self.assertEqual(
            schema["required"],  # type: ignore[index]
            [
                "version",
                "intent_units",
                "episode_bindings",
                "episode_links",
                "uncertainty",
            ],
        )

    def test_standalone_media_guidance_limits_semantic_inference(self) -> None:
        self.assertIn("low-information social cue", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("invent a semantic agenda", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "Still bind the unit to an episode as usual",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )

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

    def test_parser_merges_duplicate_episode_bindings_safely(self) -> None:
        plan = response_plan()
        plan["episode_bindings"][1].update(
            {
                "episode_ref": "new:social",
                "title": "微博浏览",
                "relation": "related",
                "topics": ["邮件"],
                "entities": ["Sakana"],
                "open_loops": ["等待邮件"],
                "salience": 0.8,
            }
        )
        plan["episode_links"] = []

        with self.assertLogs("momoi.runtime.context_planner", level="INFO") as logs:
            parsed = parse_context_plan(
                json.dumps(plan), ["event-1"], [], "turn-1", 1
            )

        self.assertEqual(len(parsed["episode_bindings"]), 1)
        binding = parsed["episode_bindings"][0]
        self.assertEqual(binding["unit_ids"], ["social", "mail"])
        self.assertEqual(binding["relation"], "primary")
        self.assertEqual(binding["topics"], ["微博", "邮件"])
        self.assertEqual(binding["entities"], ["Sakana"])
        self.assertEqual(binding["open_loops"], ["等待邮件"])
        self.assertEqual(binding["salience"], 0.8)
        self.assertTrue(
            any(
                getattr(record, "momoi_event", "") == "context_plan_normalized"
                for record in logs.records
            )
        )

        conflicting = response_plan()
        conflicting["episode_bindings"][1]["episode_ref"] = "new:social"
        conflicting["episode_links"] = []
        with self.assertRaisesRegex(ContextPlanError, "conflicting_episode_title"):
            parse_context_plan(
                json.dumps(conflicting), ["event-1"], [], "turn-1", 1
            )

        over_limit = response_plan()
        over_limit["episode_bindings"][0]["topics"] = [
            f"topic-{index}" for index in range(7)
        ]
        over_limit["episode_bindings"][1].update(
            {
                "episode_ref": "new:social",
                "title": "微博浏览",
                "topics": [f"other-{index}" for index in range(7)],
            }
        )
        over_limit["episode_links"] = []
        with self.assertRaisesRegex(
            ContextPlanError, "merged_episode_topics_limit"
        ):
            parse_context_plan(
                json.dumps(over_limit), ["event-1"], [], "turn-1", 1
            )

    def test_casual_units_can_skip_recall_without_parser_semantics(self) -> None:
        plan = response_plan()
        plan["episode_bindings"][0]["open_loops"] = ["饭后再弄"]
        parsed = parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)
        self.assertEqual(parsed["intent_units"][0]["recall_queries"], [])
        self.assertEqual(parsed["episode_bindings"][0]["open_loops"], ["饭后再弄"])

    def test_bound_episode_does_not_inject_history_or_remove_tools(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(app_config(directory))
            daemon.store.create_episode(
                "HHKB键盘电池更换",
                episode_id="hhkb",
                open_loops=["饭后再处理"],
            )
            plan = {
                "version": 1,
                "intent_units": [
                    {
                        "id": "u1",
                        "event_ids": ["event-1"],
                        "text": "先玩手机",
                        "intent": "share current activity",
                        "speech_act": "casual_share",
                        "references": ["它 -> 刚聊过的键盘"],
                        "recall_queries": [],
                    }
                ],
                "episode_bindings": [
                    {
                        "episode_id": "hhkb",
                        "is_new": False,
                        "relation": "primary",
                        "unit_ids": ["u1"],
                        "topics": [],
                        "entities": [],
                        "open_loops": [],
                        "salience": 0.5,
                    }
                ],
                "episode_links": [],
                "uncertainty": [],
            }
            self.assertEqual(
                build_plan_retrieval(daemon.store, plan, app_config(directory))[
                    "episodes"
                ],
                [],
            )
            self.assertEqual(
                [spec["name"] for spec in daemon._owner_tool_specs(plan)],
                [
                    "send_message",
                    "memory_search",
                    "conversation_search",
                    "conversation_read",
                    "memory_remember",
                    "memory_forget",
                    "goal_create",
                    "goal_update",
                    "goal_finish",
                    "goal_cancel",
                    "reminder_create",
                    "reminder_cancel",
                    "curl",
                    "read_file",
                    "write_file",
                    "apply_patch",
                    "sleep",
                    "reply_expectation_close",
                    "respond",
                ],
            )
            daemon.store.close()

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
    async def test_new_owner_message_cancels_and_restarts_context_planner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = app_config(directory)
            config = replace(
                config,
                channel=replace(config.channel, quiet_seconds=0.01),
            )
            daemon = MomoiDaemon(config)
            started = asyncio.Event()
            cancelled = asyncio.Event()

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.assertEqual(system, CONTEXT_PLANNER_SYSTEM_PROMPT)
                    self.assertEqual(_tools, [CONTEXT_PLAN_TOOL_SPEC])
                    self.assertTrue(_["require_tool"])
                    provider_self.calls += 1
                    if provider_self.calls == 1:
                        started.set()
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            cancelled.set()
                            raise
                    rendered = json.dumps(messages, ensure_ascii=False)
                    self.assertIn("第二条", rendered)
                    plan = {
                        "version": 1,
                        "intent_units": [
                            {
                                "id": "u1",
                                "event_ids": ["event-1", "event-2"],
                                "text": "第一条；第二条",
                                "intent": "combined owner update",
                                "speech_act": "casual_share",
                                "references": [],
                                "recall_queries": [],
                            }
                        ],
                        "episode_bindings": [
                            {
                                "episode_ref": "new:combined",
                                "title": "合并消息",
                                "relation": "primary",
                                "unit_ids": ["u1"],
                                "topics": [],
                                "entities": [],
                                "open_loops": [],
                                "salience": 0.2,
                            }
                        ],
                        "episode_links": [],
                        "uncertainty": [],
                    }
                    return tool_plan_response(plan)

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            first = IncomingMessage(
                "event-1", "message-1", "第一条", 1, 1, channel="napcat"
            )
            daemon.store.add_event(first)
            events = [first]
            turn_id = daemon._turn_id(first.event_id)
            daemon.store.begin_turn(turn_id, "owner", [first.event_id])
            with self.assertLogs("momoi.runtime.turns", level="INFO") as logs:
                planning = asyncio.create_task(
                    daemon._prepare_owner_context(events, turn_id)
                )
                await started.wait()
                await daemon._receive(
                    IncomingMessage(
                        "event-2",
                        "message-2",
                        "第二条",
                        2,
                        2,
                        channel="napcat",
                    )
                )
                plan, _ = await asyncio.wait_for(planning, timeout=1)

            self.assertTrue(cancelled.is_set())
            self.assertEqual(provider.calls, 2)
            self.assertEqual(len(events), 2)
            self.assertEqual(
                plan["intent_units"][0]["event_ids"], ["event-1", "event-2"]
            )
            self.assertTrue(
                any(
                    getattr(record, "momoi_event", "") == "llm_cancelled"
                    for record in logs.records
                )
            )
            daemon.store.close()

    def test_invalid_plan_log_keeps_reason_and_raw_arguments(self) -> None:
        # Runtime logging is exercised asynchronously elsewhere; keep the
        # formatter contract explicit so shadow runs remain diagnosable.
        self.assertIn("reason=last_error", Path(
            __file__
        ).parents[1].joinpath("src/momoi/runtime/turns.py").read_text())
        self.assertIn("raw_plan=raw_plan", Path(
            __file__
        ).parents[1].joinpath("src/momoi/runtime/turns.py").read_text())

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
                    return tool_plan_response(plan)

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

            with self.assertLogs("momoi.runtime.turns", level="DEBUG") as logs:
                planned = await daemon._plan_owner_context([event], turn_id)

            self.assertEqual(planned["episode_bindings"][0]["episode_id"], "old-cup")
            received = next(
                record
                for record in logs.records
                if getattr(record, "momoi_event", "") == "context_plan_received"
            )
            self.assertIn(
                "find stored drinking container",
                received.momoi_fields["intent_units"],
            )
            self.assertIn("old-cup", received.momoi_fields["episode_bindings"])
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
                        self.assertEqual(tools, [CONTEXT_PLAN_TOOL_SPEC])
                        payload = json.loads(str(messages[0]["content"]))
                        self.assertEqual(
                            payload["owner_messages"][0]["text"],
                            "刷微博，也看下之前等的邮件",
                        )
                        self.assertRegex(
                            payload["owner_messages"][0]["timestamp"],
                            r"^\d{4}-\d{2}-\d{2}T",
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
                        return tool_plan_response(response_plan())
                    provider_self.calls.append("main")
                    content = messages[0]["content"]
                    if isinstance(content, list):
                        text = "\n".join(
                            str(block.get("text") or "")
                            for block in content
                            if isinstance(block, dict)
                        )
                    else:
                        text = str(content)
                    provider_self.main_rendered = text
                    self.assertIn("<context_resolution>", text)
                    self.assertIn('"speech_act":"casual_share"', text)
                    self.assertNotIn("<context_plan>", text)
                    self.assertNotIn("browse social feed", text)
                    self.assertNotIn("episode_bindings", text)
                    self.assertNotIn('"salience"', text)
                    self.assertIn("RECENT CONTEXT 2", text)
                    self.assertNotIn("GLOBAL RAW MUST NOT LEAK", text)
                    self.assertEqual(len(messages), 1)
                    call = ToolCall(
                        "respond",
                        "respond",
                        {
                            "expects_reply": False,
                            "reply_expectation": "",
                            "mood": {"decision": "unchanged"},
                        },
                    )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage(
                "event-1",
                "1",
                "刷微博，也看下之前等的邮件",
                9999999999,
                9999999999,
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)

            self.assertEqual(provider.calls, ["planner", "main"])
            self.assertIn("RECENT CONTEXT 1", provider.planner_recent)
            self.assertIn("RECENT CONTEXT 2", provider.main_rendered)
            self.assertIn("2286-11-21T", provider.main_rendered)
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
                            self.assertIn(
                                "context_plan_tool_required",
                                str(messages[-1]["content"]),
                            )
                        return ProviderResponse(
                            [{"type": "text", "text": "not json"}], []
                        )
                    content = messages[0]["content"]
                    if isinstance(content, list):
                        text = "\n".join(
                            str(block.get("text") or "")
                            for block in content
                            if isinstance(block, dict)
                        )
                    else:
                        text = str(content)
                    self.assertNotIn("degraded_message_segment", text)
                    self.assertIn("<context_resolution>", text)
                    self.assertIn("without automatic historical recall", text)
                    call = ToolCall(
                        "respond",
                        "respond",
                        {
                            "expects_reply": False,
                            "reply_expectation": "",
                            "mood": {"decision": "unchanged"},
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
