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
    degraded_heartbeat_plan,
    degraded_context_plan,
    parse_heartbeat_plan,
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
        "version": 2,
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
        "episode_actions": [
            {
                "action": "new",
                "episode_ref": "new:social",
                "title": "微博浏览",
                "unit_ids": ["social"],
                "topics": ["微博"],
                "entities": [],
                "open_loops": [],
                "salience": 0.4,
            },
            {
                "action": "new",
                "episode_ref": "new:mail",
                "title": "邮件跟进",
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


def legacy_response_plan() -> dict[str, object]:
    plan = response_plan()
    plan["version"] = 1
    plan["episode_bindings"] = plan.pop("episode_actions")
    for binding in plan["episode_bindings"]:
        binding.pop("action")
        binding["relation"] = "primary"
    return plan


class ContextPlannerTest(unittest.TestCase):
    def test_heartbeat_plan_parser_and_degraded_fallback(self) -> None:
        plan = parse_heartbeat_plan(
            {
                "version": 1,
                "activity": {
                    "intent": "浏览微博关注流",
                    "reason": "看看最近感兴趣的动态",
                    "recall_queries": ["微博登录错误报告规则"],
                },
                "uncertainty": [],
            }
        )
        self.assertEqual(plan["activity"]["recall_queries"], ["微博登录错误报告规则"])
        with self.assertRaisesRegex(ContextPlanError, "invalid_heartbeat_activity"):
            parse_heartbeat_plan(
                {"version": 1, "activity": {}, "uncertainty": []}
            )
        degraded = degraded_heartbeat_plan("", "invalid_json")
        self.assertEqual(degraded["activity"]["recall_queries"], [])
        self.assertEqual(degraded["activity"]["intent"], "spend time freely")

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
                "episode_actions",
                "episode_links",
                "uncertainty",
            ],
        )
        action_shapes = schema["properties"]["episode_actions"]["items"]["oneOf"]  # type: ignore[index]
        self.assertEqual(
            [shape["properties"]["action"]["enum"][0] for shape in action_shapes],
            ["none", "continue", "new"],
        )

    def test_standalone_media_guidance_limits_semantic_inference(self) -> None:
        self.assertIn("low-information social cue", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("Do not invent an agenda", CONTEXT_PLANNER_SYSTEM_PROMPT)

    def test_planner_guidance_preserves_capability_while_discouraging_noise(
        self,
    ) -> None:
        self.assertIn(
            "Default to one intent unit per semantic goal",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Recent Turns are the first source of continuity",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("active_recent_turn_ids", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "interrupted_reply_expectation", CONTEXT_PLANNER_SYSTEM_PROMPT
        )
        self.assertIn(
            "their presence alone is not a reason to",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("omitted `kind` means `owner`", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "omitted message `delivery` means", CONTEXT_PLANNER_SYSTEM_PROMPT
        )
        self.assertIn("intent_indexes", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("structured `truncated` preview", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("compact final state", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("tool calls, results", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "correction may invalidate an older persisted fact",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("queued`, `failed`, and", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("causal completeness", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "only for genuinely independent evidence needs",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("`|`-separated OR expression", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("people, events, places, preferences", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("any other topic", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("Do not include file paths", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "could change the reply, recall target, or Episode action",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        schema = CONTEXT_PLAN_TOOL_SPEC["input_schema"]  # type: ignore[assignment]
        unit = schema["properties"]["intent_units"]["items"]  # type: ignore[index]
        self.assertEqual(unit["properties"]["recall_queries"]["maxItems"], 2)
        self.assertEqual(schema["properties"]["uncertainty"]["maxItems"], 4)  # type: ignore[index]

    def test_parser_requires_event_coverage_and_normalizes_episode_refs(self) -> None:
        plan = response_plan()
        parsed = parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)
        bindings = parsed["episode_actions"]
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

    def test_links_may_target_unbound_candidates_but_not_unknown_episodes(self) -> None:
        plan = response_plan()
        plan["episode_links"] = [
            {
                "from_episode_ref": "new:mail",
                "to_episode_ref": "older-game",
                "kind": "references",
            }
        ]
        parsed = parse_context_plan(
            plan,
            ["event-1"],
            [{"id": "older-game"}],
            "turn-1",
            1,
        )
        self.assertEqual(
            parsed["episode_links"][0]["to_episode_id"], "older-game"
        )

        plan["episode_links"][0]["to_episode_ref"] = "not-a-candidate"
        with self.assertRaisesRegex(ContextPlanError, "unknown_link_episode"):
            parse_context_plan(
                plan,
                ["event-1"],
                [{"id": "older-game"}],
                "turn-1",
                1,
            )

    def test_new_episode_refs_require_ascii_slugs(self) -> None:
        plan = response_plan()
        plan["episode_actions"][0]["episode_ref"] = "new:一起玩游戏"
        with self.assertRaisesRegex(ContextPlanError, "invalid_new_episode_ref"):
            parse_context_plan(plan, ["event-1"], [], "turn-1", 1)

    def test_v2_allows_none_and_continue_without_changing_existing_title(
        self,
    ) -> None:
        plan = response_plan()
        plan["episode_actions"] = [
            {"action": "none", "unit_ids": ["social"]},
            {
                "action": "continue",
                "episode_ref": "mail-thread",
                "unit_ids": ["mail"],
                "topics": ["新进展"],
                "entities": [],
                "open_loops": [],
                "salience": 0.6,
            },
        ]
        plan["episode_links"] = []

        parsed = parse_context_plan(
            plan,
            ["event-1"],
            [{"id": "mail-thread", "title": "已有邮件话题"}],
            "turn-1",
            1,
        )

        self.assertEqual(parsed["episode_actions"][0]["action"], "none")
        self.assertEqual(parsed["episode_actions"][1]["episode_id"], "mail-thread")
        self.assertEqual(
            parsed["episode_actions"][1]["title"], "已有邮件话题"
        )

    def test_v2_requires_each_unit_exactly_once(self) -> None:
        missing = response_plan()
        missing["episode_actions"] = [missing["episode_actions"][0]]
        missing["episode_links"] = []
        with self.assertRaisesRegex(ContextPlanError, "unbound_intent_units"):
            parse_context_plan(missing, ["event-1"], [], "turn-1", 1)

        duplicate = response_plan()
        duplicate["episode_actions"][0]["unit_ids"] = ["social", "mail"]
        duplicate["episode_actions"][1]["unit_ids"] = ["mail"]
        duplicate["episode_links"] = []
        with self.assertRaisesRegex(ContextPlanError, "duplicate_binding_unit"):
            parse_context_plan(duplicate, ["event-1"], [], "turn-1", 1)

    def test_v1_parser_merges_duplicate_episode_bindings_safely(self) -> None:
        plan = legacy_response_plan()
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

        conflicting = legacy_response_plan()
        conflicting["episode_bindings"][1]["episode_ref"] = "new:social"
        conflicting["episode_links"] = []
        with self.assertRaisesRegex(ContextPlanError, "conflicting_episode_title"):
            parse_context_plan(
                json.dumps(conflicting), ["event-1"], [], "turn-1", 1
            )

        over_limit = legacy_response_plan()
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
        plan["episode_actions"][0]["open_loops"] = ["饭后再弄"]
        parsed = parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)
        self.assertEqual(parsed["intent_units"][0]["recall_queries"], [])
        self.assertEqual(parsed["episode_actions"][0]["open_loops"], ["饭后再弄"])

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
                    "thinking_search",
                    "thinking_read",
                    "goal_create",
                    "goal_update",
                    "goal_finish",
                    "goal_cancel",
                    "reminder_create",
                    "reminder_cancel",
                    "curl",
                    "read_file",
                    "list_dir",
                    "write_file",
                    "apply_patch",
                    "sleep",
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
        self.assertTrue(
            all(item["action"] == "none" for item in plan["episode_actions"])
        )
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
                            list(payload),
                            [
                                "candidate_goals",
                                "candidate_reminders",
                                "recent_turns",
                                "active_recent_turn_ids",
                                "candidate_episodes",
                                "interrupted_reply_expectation",
                                "owner_messages",
                            ],
                        )
                        self.assertEqual(
                            payload["owner_messages"][0]["text"],
                            "刷微博，也看下之前等的邮件",
                        )
                        self.assertRegex(
                            payload["owner_messages"][0]["timestamp"],
                            r"^\d{4}-\d{2}-\d{2}T",
                        )
                        recent = json.dumps(
                            payload["recent_turns"], ensure_ascii=False
                        )
                        provider_self.planner_recent = recent
                        self.assertIn("RECENT CONTEXT 1", recent)
                        self.assertIn("RECENT CONTEXT 2", recent)
                        self.assertIn("GLOBAL RAW MUST NOT LEAK", recent)
                        self.assertEqual(
                            payload["active_recent_turn_ids"],
                            ["recent-turn-1", "recent-turn-2"],
                        )
                        self.assertIsNone(
                            payload["interrupted_reply_expectation"]
                        )
                        self.assertEqual(len(payload["candidate_goals"]), 8)
                        self.assertEqual(
                            payload["candidate_goals"][0]["id"], "goal-8"
                        )
                        self.assertEqual(len(payload["candidate_reminders"]), 8)
                        self.assertEqual(
                            payload["candidate_reminders"][0]["id"], "reminder-8"
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
                    self.assertIn("<recent_turns>", text)
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
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT COUNT(*) FROM episode_turns WHERE turn_id=?", (turn_id,)
                ).fetchone()[0],
                0,
            )
            daemon.store.close()
