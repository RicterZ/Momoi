import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_assembler import build_plan_retrieval
from momoi.runtime.context_planner import (
    CONTEXT_PLAN_TOOL_NAME,
    CONTEXT_PLAN_TOOL_SPEC,
    HEARTBEAT_PLAN_TOOL_SPEC,
    ContextPlanError,
    degraded_heartbeat_plan,
    degraded_context_plan,
    parse_heartbeat_plan,
    parse_context_plan,
)
from momoi.runtime.context_service import render_heartbeat_planner_request
from momoi.runtime.turns import (
    CONTEXT_PLANNER_SYSTEM_PROMPT,
    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
)
from momoi.runtime.turn_support import conversation_guidance
from tests.support import planner_sections


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
            },
            {
                "id": "mail",
                "event_ids": ["event-1"],
                "text": "看邮件",
                "intent": "check mail",
                "speech_act": "request",
                "references": ["之前等的邮件"],
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
        "owner_handoff": {
            "context": {
                "status": "sufficient",
                "needs": [],
                "reason": "当前上下文足够",
            },
            "mcp": {"servers": [], "reason": "不需要外部服务"},
            "execution": {
                "mode": "work",
                "outline": ["处理主人当前请求"],
                "reason": "按当前意图执行",
            },
        },
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
    def test_heartbeat_planner_request_is_tagged_text_with_fixed_memory(self) -> None:
        rendered = render_heartbeat_planner_request(
            mcp_servers=[{"id": "weibo", "description": "Browse Weibo"}],
            workspace_guidance="Choose one activity.",
            long_term_memories="- owner likes short replies",
            recent_memories="- shared game night",
            active_goals="- id=g1 status=active title=goal",
            pending_reminders="(none)",
            recent_topics=[{"title": "Game", "topics": ["BA"]}],
            recent_conversation="owner: hello",
            recent_heartbeat_activities=[{"at": "now", "text": "rest"}],
            previous_activity={"activity": "rest", "result": "quiet"},
            current_self_state='{"mood":{"state":"calm"}}',
            conversation_state={"owner_event_revision": 1},
            current_time="2026-08-20T20:00:00+08:00",
        )
        self.assertTrue(rendered.startswith("<available_mcp_servers>"))
        self.assertIn("<long_term_memories>\n- owner likes short replies", rendered)
        self.assertIn("<recent_memories>\n- shared game night", rendered)
        self.assertIn("<recent_topics>\n- title=Game topics=BA", rendered)
        self.assertNotIn('"available_mcp_servers"', rendered)

    def test_mcp_catalog_uses_server_capability_descriptions(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.mcp = SimpleNamespace(
            configs={
                "gog": {
                    "description": "Search Gmail and use Google Calendar.",
                }
            },
            tool_specs=[
                {
                    "name": "mcp__gog__gmail_search",
                    "description": "Search Gmail",
                    "input_schema": {"type": "object"},
                }
            ],
        )

        catalog = daemon._mcp_server_catalog()

        self.assertEqual(
            catalog,
            [
                {
                    "id": "gog",
                    "description": "Search Gmail and use Google Calendar.",
                }
            ],
        )
        self.assertNotIn("sample_tools", catalog[0])
        self.assertNotIn("tool_count", catalog[0])

    def test_heartbeat_plan_parser_and_degraded_fallback(self) -> None:
        schema = HEARTBEAT_PLAN_TOOL_SPEC["input_schema"]
        self.assertEqual(
            schema["required"],
            ["version", "activity", "heartbeat_handoff", "uncertainty"],
        )
        self.assertIn(
            "recall_queries",
            schema["properties"]["activity"]["properties"],
        )
        self.assertIn("recall_queries", HEARTBEAT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("recent_heartbeat_activities", HEARTBEAT_PLANNER_SYSTEM_PROMPT)
        plan = parse_heartbeat_plan(
            {
                "version": 2,
                "activity": {
                    "intent": "浏览微博关注流",
                    "reason": "看看最近感兴趣的动态",
                    "recall_queries": ["微博 登录规则", "最近关注的游戏"],
                },
                "heartbeat_handoff": {
                    "context": {
                        "status": "lookup_required",
                        "needs": [
                            {
                                "tool": "memory_search",
                                "query": "微博登录错误报告规则",
                                "evidence": "relevant_history",
                            }
                        ],
                        "reason": "执行活动需要已知规则",
                    },
                    "mcp": {
                        "servers": ["weibo"],
                        "reason": "计划浏览微博关注流",
                    },
                    "execution": {
                        "mode": "work",
                        "outline": ["查询已知规则", "浏览关注流", "核对结果"],
                        "reason": "需要实际执行浏览活动",
                    },
                },
                "uncertainty": [],
            },
            {"weibo", "gog"},
        )
        self.assertEqual(
            plan["heartbeat_handoff"]["context"]["needs"][0]["query"],
            "微博登录错误报告规则",
        )
        self.assertEqual(
            plan["heartbeat_handoff"]["mcp"]["servers"], ["weibo"]
        )
        self.assertEqual(
            plan["activity"]["recall_queries"],
            ["微博 登录规则", "最近关注的游戏"],
        )
        with self.assertRaisesRegex(ContextPlanError, "invalid_heartbeat_plan"):
            parse_heartbeat_plan(
                {"version": 1, "activity": {}, "uncertainty": []}
            )
        degraded = degraded_heartbeat_plan("", "invalid_json")
        self.assertEqual(degraded["activity"]["intent"], "spend time freely")
        self.assertEqual(
            degraded["heartbeat_handoff"]["execution"]["mode"], "rest"
        )
        self.assertEqual(
            degraded["heartbeat_handoff"]["mcp"]["servers"], []
        )
        invalid_rest = json.loads(json.dumps(degraded, ensure_ascii=False))
        invalid_rest["heartbeat_handoff"]["execution"]["outline"] = ["偷偷工作"]
        with self.assertRaisesRegex(
            ContextPlanError, "invalid_heartbeat_execution"
        ):
            parse_heartbeat_plan(invalid_rest)

    def test_context_plan_shape_lives_in_tool_schema(self) -> None:
        self.assertIn(CONTEXT_PLAN_TOOL_NAME, CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn('"intent_units"', CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("return shape", CONTEXT_PLANNER_SYSTEM_PROMPT)
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
                "owner_handoff",
                "uncertainty",
            ],
        )
        action_shapes = schema["properties"]["episode_actions"]["items"]["oneOf"]  # type: ignore[index]
        self.assertEqual(
            [shape["properties"]["action"]["enum"][0] for shape in action_shapes],
            ["none", "continue", "new"],
        )

    def test_context_plan_selects_only_available_mcp_servers(self) -> None:
        plan = response_plan()
        plan["owner_handoff"] = {
            "context": {
                "status": "sufficient",
                "needs": [],
                "reason": "当前上下文足够",
            },
            "mcp": {
                "servers": ["gog"],
                "reason": "主人明确要求检查邮件",
            },
            "execution": {
                "mode": "work",
                "outline": ["搜索邮件", "核对结果", "回复老师"],
                "reason": "需要完成邮件检查",
            },
        }
        parsed = parse_context_plan(
            plan,
            ["event-1"],
            [],
            "turn-1",
            1,
            {"homeassistant", "gog"},
        )
        self.assertEqual(
            parsed["owner_handoff"]["mcp"]["servers"], ["gog"]
        )
        self.assertIn("邮件", parsed["owner_handoff"]["mcp"]["reason"])

        plan["owner_handoff"]["mcp"]["servers"] = ["missing"]
        with self.assertRaisesRegex(ContextPlanError, "unknown_mcp_server"):
            parse_context_plan(
                plan,
                ["event-1"],
                [],
                "turn-1",
                1,
                {"homeassistant", "gog"},
            )

        plan["owner_handoff"]["mcp"]["servers"] = []
        plan["owner_handoff"]["context"] = {
            "status": "lookup_required",
            "needs": [
                {
                    "tool": "conversation_search",
                    "query": "老师之前对晚餐的原话",
                    "evidence": "exact_wording",
                }
            ],
            "reason": "摘要不足以证明精确原话",
        }
        parsed = parse_context_plan(
            plan,
            ["event-1"],
            [],
            "turn-1",
            1,
            {"gog"},
        )
        self.assertEqual(
            parsed["owner_handoff"]["context"]["needs"][0]["tool"],
            "conversation_search",
        )
        guidance = conversation_guidance(parsed)
        self.assertFalse(guidance.lstrip().startswith("{"))
        self.assertIn("Owner intent 1", guidance)
        self.assertIn("speech act: casual_share", guidance)
        self.assertIn("Context handoff", guidance)
        self.assertIn("status: lookup_required", guidance)
        self.assertIn("tool=conversation_search", guidance)

    def test_standalone_media_guidance_limits_semantic_inference(self) -> None:
        self.assertIn("low-information social cue", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("Do not invent an agenda", CONTEXT_PLANNER_SYSTEM_PROMPT)

    def test_planner_guidance_preserves_capability_while_discouraging_noise(
        self,
    ) -> None:
        self.assertIn("## Planning process", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("Assess whether that baseline, supplied Recent Turns", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("active` or `background", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "interrupted_reply_expectation", CONTEXT_PLANNER_SYSTEM_PROMPT
        )
        self.assertIn(
            "older Turns are used only for",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("omitted `kind` means owner", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "omitted message `delivery` means", CONTEXT_PLANNER_SYSTEM_PROMPT
        )
        self.assertIn("intent_indexes", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("`truncated` result", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "regardless of active focus",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("State-changing tools use compact", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "must call anything beyond `send_message`/`respond`",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("context.needs", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("conversation_search", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("thinking_search", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("advisory evidence/action outline", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("recall_queries", CONTEXT_PLANNER_SYSTEM_PROMPT)
        schema = CONTEXT_PLAN_TOOL_SPEC["input_schema"]  # type: ignore[assignment]
        unit = schema["properties"]["intent_units"]["items"]  # type: ignore[index]
        self.assertIn("recall_queries", unit["properties"])
        self.assertEqual(schema["properties"]["uncertainty"]["maxItems"], 4)  # type: ignore[index]
        legacy_keyword_plan = response_plan()
        legacy_keyword_plan["intent_units"][0]["recall_queries"] = ["旧关键词"]
        parsed = parse_context_plan(
            legacy_keyword_plan,
            ["event-1"],
            [],
            "turn-1",
            1,
        )
        self.assertEqual(parsed["intent_units"][0]["recall_queries"], ["旧关键词"])

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

    def test_parser_rejects_version_one_plans(self) -> None:
        plan = response_plan()
        plan["version"] = 1
        with self.assertRaisesRegex(ContextPlanError, "unsupported_version"):
            parse_context_plan(plan, ["event-1"], [], "turn-1", 1)

    def test_casual_units_can_skip_recall_without_parser_semantics(self) -> None:
        plan = response_plan()
        plan["episode_actions"][0]["open_loops"] = ["饭后再弄"]
        parsed = parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)
        self.assertNotIn("recall_queries", parsed["intent_units"][0])
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
                "version": 2,
                "intent_units": [
                    {
                        "id": "u1",
                        "event_ids": ["event-1"],
                        "text": "先玩手机",
                        "intent": "share current activity",
                        "speech_act": "casual_share",
                        "references": ["它 -> 刚聊过的键盘"],
                    }
                ],
                "episode_actions": [
                    {
                        "action": "continue",
                        "episode_ref": "hhkb",
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
                    "tool_enable",
                    "respond",
                ],
            )
            routed = {
                **plan,
                "owner_handoff": {
                    "context": {
                        "status": "sufficient",
                        "needs": [],
                        "reason": "上下文足够",
                    },
                    "mcp": {"servers": [], "reason": "不需要外部服务"},
                    "execution": {
                        "mode": "respond",
                        "outline": ["回复老师"],
                        "reason": "普通回应",
                    },
                },
            }
            self.assertEqual(
                [spec["name"] for spec in daemon._owner_tool_specs(routed)],
                [
                    "send_message",
                    "tool_enable",
                    "respond",
                ],
            )
            lookup = {
                **routed,
                "owner_handoff": {
                    **routed["owner_handoff"],
                    "context": {
                        "status": "lookup_required",
                        "needs": [
                            {
                                "tool": "conversation_search",
                                "query": "键盘",
                                "evidence": "unresolved_reference",
                            }
                        ],
                        "reason": "需要查找旧对话",
                    },
                },
            }
            self.assertEqual(
                [spec["name"] for spec in daemon._owner_tool_specs(lookup)],
                [
                    "send_message",
                    "conversation_search",
                    "tool_enable",
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
                        "version": 2,
                        "intent_units": [
                            {
                                "id": "u1",
                                "event_ids": ["event-1", "event-2"],
                                "text": "第一条；第二条",
                                "intent": "combined owner update",
                                "speech_act": "casual_share",
                                "references": [],
                            }
                        ],
                        "episode_actions": [
                            {
                                "action": "new",
                                "episode_ref": "new:combined",
                                "title": "合并消息",
                                "unit_ids": ["u1"],
                                "topics": [],
                                "entities": [],
                                "open_loops": [],
                                "salience": 0.2,
                            }
                        ],
                        "episode_links": [],
                        "owner_handoff": {
                            "context": {
                                "status": "sufficient",
                                "needs": [],
                                "reason": "当前上下文足够",
                            },
                            "mcp": {
                                "servers": [],
                                "reason": "不需要外部服务",
                            },
                            "execution": {
                                "mode": "respond",
                                "outline": ["处理合并后的主人消息"],
                                "reason": "当前输入已足够回应",
                            },
                        },
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
                    payload = planner_sections(str(messages[0]["content"]))
                    self.assertIn("id=old-cup", payload["candidate_episodes"])
                    plan = {
                        "version": 2,
                        "intent_units": [
                            {
                                "id": "u1",
                                "event_ids": ["semantic-event"],
                                "text": "我把喝水用的东西搁哪儿啦",
                                "intent": "find stored drinking container",
                                "speech_act": "question",
                                "references": ["喝水用的东西"],
                            }
                        ],
                        "episode_actions": [
                            {
                                "action": "continue",
                                "episode_ref": "old-cup",
                                "unit_ids": ["u1"],
                                "topics": ["保温杯", "阁楼收纳"],
                                "entities": [],
                                "open_loops": [],
                                "salience": 0.7,
                            }
                        ],
                        "episode_links": [],
                        "owner_handoff": {
                            "context": {
                                "status": "lookup_required",
                                "needs": [
                                    {
                                        "tool": "conversation_search",
                                        "query": "保温杯 阁楼 收纳位置",
                                        "evidence": "unresolved_reference",
                                    }
                                ],
                                "reason": "需要查历史收纳位置",
                            },
                            "mcp": {
                                "servers": [],
                                "reason": "不需要外部服务",
                            },
                            "execution": {
                                "mode": "respond",
                                "outline": ["查找收纳位置", "根据证据回答"],
                                "reason": "主人在问旧物品位置",
                            },
                        },
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

            self.assertEqual(planned["episode_actions"][0]["episode_id"], "old-cup")
            received = next(
                record
                for record in logs.records
                if getattr(record, "momoi_event", "") == "context_plan_received"
            )
            self.assertIn(
                "find stored drinking container",
                received.momoi_fields["intent_units"],
            )
            self.assertIn("old-cup", received.momoi_fields["episode_actions"])
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
            memory_now = time.time()
            with daemon.store._db:
                daemon.store._db.executemany(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('profile', ?, ?, ?, 'owner', 'source', 'evidence',
                               0.8, ?, ?)""",
                    [
                        ("fixed.style", "长期记忆：喜欢简短回复", "always", memory_now, memory_now),
                        ("recent.mail", "近期记忆：正在等待邮件", "recent", memory_now, memory_now),
                    ],
                )
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
                        payload = planner_sections(str(messages[0]["content"]))
                        self.assertEqual(
                            list(payload),
                            [
                                "available_mcp_servers",
                                "long_term_memories",
                                "recent_memories",
                                "candidate_goals",
                                "candidate_reminders",
                                "recent_turns",
                                "recent_conversation",
                                "candidate_episodes",
                                "interrupted_reply_expectation",
                                "owner_messages",
                            ],
                        )
                        self.assertIn(
                            "刷微博，也看下之前等的邮件",
                            payload["owner_messages"],
                        )
                        self.assertRegex(
                            payload["owner_messages"],
                            r"at=\d{4}-\d{2}-\d{2}T",
                        )
                        recent = str(payload["recent_turns"])
                        provider_self.planner_recent = recent
                        self.assertIn("RECENT CONTEXT 1", recent)
                        self.assertIn("RECENT CONTEXT 2", recent)
                        self.assertIn("GLOBAL RAW MUST NOT LEAK", recent)
                        self.assertEqual(
                            payload["interrupted_reply_expectation"], "(none)"
                        )
                        self.assertIn("喜欢简短回复", payload["long_term_memories"])
                        self.assertIn("正在等待邮件", payload["recent_memories"])
                        self.assertEqual(payload["candidate_goals"].count("id="), 8)
                        self.assertIn("id=goal-8", payload["candidate_goals"])
                        self.assertNotIn("id=goal-0", payload["candidate_goals"])
                        self.assertEqual(
                            payload["candidate_reminders"].count("id="), 8
                        )
                        self.assertIn("id=reminder-8", payload["candidate_reminders"])
                        self.assertNotIn("id=reminder-0", payload["candidate_reminders"])
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
                    self.assertIn("<long_term_memories>", text)
                    self.assertIn("喜欢简短回复", text)
                    self.assertIn("<recent_memories>", text)
                    self.assertIn("正在等待邮件", text)
                    self.assertIn("speech act: casual_share", text)
                    resolution = text.split("<context_resolution>\n", 1)[1].split(
                        "\n</context_resolution>", 1
                    )[0]
                    self.assertFalse(resolution.lstrip().startswith("{"))
                    self.assertNotIn("<context_plan>", text)
                    self.assertIn("browse social feed", text)
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
            self.assertEqual(stored["retrieval"]["version"], 3)
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
                    self.assertIn("degraded_message_segment", text)
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
