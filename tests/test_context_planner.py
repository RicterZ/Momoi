import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from momoi.agenda_tools import AGENDA_TOOL_SPECS
from momoi.builtin_tools import BUILTIN_TOOL_SPECS
from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.memory_tools import MEMORY_TOOL_SPECS
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
from momoi.runtime.context_service import (
    PLANNER_INTERNAL_TOOLS,
    render_heartbeat_planner_request,
)
from momoi.runtime.turn_support import (
    CONTEXT_PLANNER_PROTOCOL_PROMPT,
    CONTEXT_PLANNER_SYSTEM_PROMPT,
    DOWNSTREAM_OWNER_CONTRACT_PROMPT,
    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
    STYLE_CARD_SYSTEM_PROMPT,
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
        "version": 3,
        "intent_units": [
            {
                "id": "social",
                "event_ids": ["event-1"],
                "text": "刷微博",
                "intent": "browse social feed",
                "speech_act": "casual_share",
                "references": [],
                "recall_mode": "search",
                "recall_queries": ["微博"],
            },
            {
                "id": "mail",
                "event_ids": ["event-1"],
                "text": "看邮件",
                "intent": "check mail",
                "speech_act": "request",
                "references": ["之前等的邮件"],
                "recall_mode": "search",
                "recall_queries": ["等待中的邮件"],
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
        "handoff": {
            "context_status": "sufficient",
            "context_needs": [],
            "context_reason": "当前上下文足够",
            "mcp_servers": [],
            "mcp_reason": "不需要外部服务",
            "execution_mode": "work",
            "execution_outline": ["处理主人当前请求"],
            "execution_reason": "按当前意图执行",
            "delivery_mode": "bubbles",
            "delivery_bubbles": [
                {
                    "timing": "after the requested work",
                    "form": "complete",
                    "purpose": "report the verified result",
                }
            ],
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
            internal_tools=[{"id": "memory_search", "description": "Search memory"}],
            mcp_servers=[{"id": "weibo", "description": "Browse Weibo"}],
            workspace_guidance="Choose one activity.",
            long_term_memories="- owner likes short replies",
            recent_memories="- shared game night",
            active_goals="- id=g1 status=active title=goal",
            pending_reminders="(none)",
            recent_topics=[{"title": "Game", "topics": ["BA"]}],
            recent_turns={
                "turns": [
                    {
                        "turn_id": "t1",
                        "at": "2026-08-20T20:00:00+08:00",
                        "timeline": [{"type": "owner_message", "text": "hello"}],
                    }
                ]
            },
            recent_turn_base_count=1,
            active_recent_turn_ids=["t1"],
            recent_heartbeat_activities=[{"at": "now", "text": "rest"}],
            previous_activity={"activity": "rest", "result": "quiet"},
            current_self_state='{"mood":{"state":"calm"}}',
            conversation_state={"owner_event_revision": 1},
            current_time="2026-08-20T20:00:00+08:00",
        )
        self.assertTrue(rendered.startswith("<available_internal_tools>"))
        self.assertIn("id=memory_search", rendered)
        self.assertIn("<long_term_memories>\n- owner likes short replies", rendered)
        self.assertIn("<recent_memories>\n- shared game night", rendered)
        self.assertIn("<recent_topics>\n- title=Game topics=BA", rendered)
        self.assertIn("<recent_turn_base>\nT-1", rendered)
        self.assertIn("<recent_turn_focus>\nT-1", rendered)
        self.assertLess(
            rendered.index("<available_internal_tools>"),
            rendered.index("<available_mcp_servers>"),
        )
        self.assertLess(
            rendered.index("<recent_memories>"),
            rendered.index("<active_goals>"),
        )
        self.assertLess(
            rendered.index("<pending_reminders>"),
            rendered.index("<recent_turn_base>"),
        )
        self.assertLess(
            rendered.index("<recent_turn_base>"),
            rendered.index("<recent_turn_append>"),
        )
        self.assertLess(
            rendered.index("<recent_turn_focus>"),
            rendered.index("<current_time>"),
        )
        self.assertNotIn("<recent_conversation>", rendered)
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
        self.assertIn(
            "recall_queries",
            schema["properties"]["activity"]["required"],
        )
        self.assertIn("recall_queries", HEARTBEAT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("recent_heartbeat_activities", HEARTBEAT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("recent_turn_base", HEARTBEAT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("recent_turn_focus", HEARTBEAT_PLANNER_SYSTEM_PROMPT)
        plan = parse_heartbeat_plan(
            {
                "version": 2,
                "activity": {
                    "intent": "浏览微博关注流",
                    "reason": "看看最近感兴趣的动态",
                    "recall_queries": ["微博 | 登录规则", "最近关注的游戏"],
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
        self.assertEqual(plan["heartbeat_handoff"]["mcp"]["servers"], ["weibo"])
        self.assertEqual(
            plan["activity"]["recall_queries"],
            ["微博|登录规则", "最近关注的游戏"],
        )
        with self.assertRaisesRegex(ContextPlanError, "invalid_heartbeat_plan"):
            parse_heartbeat_plan({"version": 1, "activity": {}, "uncertainty": []})
        degraded = degraded_heartbeat_plan("", "invalid_json")
        self.assertEqual(degraded["activity"]["intent"], "spend time freely")
        self.assertEqual(degraded["heartbeat_handoff"]["execution"]["mode"], "rest")
        self.assertEqual(degraded["heartbeat_handoff"]["mcp"]["servers"], [])
        invalid_rest = json.loads(json.dumps(degraded, ensure_ascii=False))
        invalid_rest["heartbeat_handoff"]["execution"]["outline"] = ["偷偷工作"]
        with self.assertRaisesRegex(ContextPlanError, "invalid_heartbeat_execution"):
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
                "handoff",
                "uncertainty",
            ],
        )
        self.assertEqual(
            schema["properties"]["episode_actions"]["items"]["properties"][  # type: ignore[index]
                "action"
            ]["enum"],
            ["none", "continue", "new"],
        )
        self.assertNotIn(
            "oneOf",
            schema["properties"]["episode_actions"]["items"],  # type: ignore[index]
        )
        episode_description = schema["properties"]["episode_actions"]["items"][  # type: ignore[index]
            "description"
        ]
        self.assertIn("continue also needs episode_ref", episode_description)
        status_description = schema["properties"]["handoff"]["properties"][  # type: ignore[index]
            "context_status"
        ]["description"]
        self.assertIn("sufficient with no context_needs", status_description)
        self.assertNotIn("none needs only", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn("sufficient` requires empty", CONTEXT_PLANNER_SYSTEM_PROMPT)

    def test_context_plan_selects_only_available_mcp_servers(self) -> None:
        plan = response_plan()
        plan["handoff"] = {
            "context_status": "sufficient",
            "context_needs": [],
            "context_reason": "当前上下文足够",
            "mcp_servers": ["gog"],
            "mcp_reason": "主人明确要求检查邮件",
            "execution_mode": "work",
            "execution_outline": ["搜索邮件", "核对结果", "回复老师"],
            "execution_reason": "需要完成邮件检查",
            "delivery_mode": "bubbles",
            "delivery_bubbles": [
                {
                    "timing": "after the requested work",
                    "form": "complete",
                    "purpose": "report the verified result",
                }
            ],
        }
        parsed = parse_context_plan(
            plan,
            ["event-1"],
            [],
            "turn-1",
            1,
            {"homeassistant", "gog"},
        )
        self.assertEqual(parsed["owner_handoff"]["mcp"]["servers"], ["gog"])
        self.assertIn("邮件", parsed["owner_handoff"]["mcp"]["reason"])

        plan["handoff"]["mcp_servers"] = ["missing"]
        with self.assertRaisesRegex(ContextPlanError, "unknown_mcp_server"):
            parse_context_plan(
                plan,
                ["event-1"],
                [],
                "turn-1",
                1,
                {"homeassistant", "gog"},
            )

        plan["handoff"]["mcp_servers"] = []
        plan["handoff"]["context_status"] = "lookup_required"
        plan["handoff"]["context_needs"] = [
            {
                "tool": "conversation_search",
                "query": "老师之前对晚餐的原话",
                "evidence": "exact_wording",
            }
        ]
        plan["handoff"]["context_reason"] = "摘要不足以证明精确原话"
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
        self.assertIn("Delivery handoff", guidance)
        self.assertIn(
            "bubble 1: timing=after the requested work form=complete "
            "purpose=report the verified result",
            guidance,
        )

    def test_standalone_media_guidance_limits_semantic_inference(self) -> None:
        self.assertIn("low-information social cue", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("Do not invent an agenda", CONTEXT_PLANNER_SYSTEM_PROMPT)

    def test_planner_guidance_preserves_capability_while_discouraging_noise(
        self,
    ) -> None:
        self.assertIn("## Planning process", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "Evaluate the fixed memory baseline", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn("`recent_turn_base`", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("`recent_turn_append`", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("`recent_turn_focus`", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "Older supplied Turns are background evidence",
            CONTEXT_PLANNER_PROTOCOL_PROMPT,
        )
        self.assertIn("omitted `kind` means owner", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "omitted message `delivery` means", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn("intent_indexes", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("`truncated` result", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "regardless of active focus",
            CONTEXT_PLANNER_PROTOCOL_PROMPT,
        )
        self.assertIn("State-changing tools use", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("handoff.context_needs", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("conversation_search", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("thinking_search", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("owner-visible delivery", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "one short utterance per\n   item", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn(
            "successive clauses of the same reply as successive items",
            CONTEXT_PLANNER_PROTOCOL_PROMPT,
        )
        self.assertIn(
            "judged fresh from the current move", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn(
            "explicit `recall_mode` decision", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn("not its social tone", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("even inside casual sharing", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "subject of the owner's impression", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn(
            "a factual question is not required", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn("a miss\n   seems likely", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("presumed downstream persona", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn("is not supplied evidence", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "skip` contradicts that uncertainty", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn(
            "route the relevant public-search server", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn("only genuine aliases", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "broader topic as OR alternatives", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        self.assertIn("without surrounding spaces", CONTEXT_PLANNER_PROTOCOL_PROMPT)
        self.assertIn(
            "internal-recall/private-name/public-search",
            CONTEXT_PLANNER_PROTOCOL_PROMPT,
        )
        self.assertIn(
            "unfamiliar public person",
            DOWNSTREAM_OWNER_CONTRACT_PROMPT,
        )
        self.assertIn("private nickname", DOWNSTREAM_OWNER_CONTRACT_PROMPT)
        schema = CONTEXT_PLAN_TOOL_SPEC["input_schema"]  # type: ignore[assignment]
        unit = schema["properties"]["intent_units"]["items"]  # type: ignore[index]
        self.assertIn("recall_mode", unit["properties"])
        self.assertIn("recall_queries", unit["properties"])
        self.assertIn("recall_mode", unit["required"])
        self.assertIn("recall_queries", unit["required"])
        self.assertEqual(unit["properties"]["recall_mode"]["enum"], ["search", "skip"])
        self.assertIn(
            "without surrounding spaces",
            unit["properties"]["recall_queries"]["description"],
        )
        self.assertEqual(schema["properties"]["uncertainty"]["maxItems"], 4)  # type: ignore[index]
        self.assertIn(
            "Missing recallable identity",
            schema["properties"]["uncertainty"]["description"],  # type: ignore[index]
        )
        normalized_query_plan = response_plan()
        normalized_query_plan["intent_units"][0]["recall_queries"] = [
            "旧关键词 | 旧别名"
        ]
        parsed = parse_context_plan(
            normalized_query_plan,
            ["event-1"],
            [],
            "turn-1",
            1,
        )
        self.assertEqual(
            parsed["intent_units"][0]["recall_queries"],
            ["旧关键词|旧别名"],
        )

    def test_parser_requires_explicit_recall_decision_by_information_need(self) -> None:
        plan = response_plan()
        parsed = parse_context_plan(plan, ["event-1"], [], "turn-1", 1)
        self.assertEqual(
            parsed["intent_units"][0]["recall"],
            {"mode": "search", "queries": ["微博"]},
        )

        social = response_plan()
        social["intent_units"] = [social["intent_units"][0]]
        social["intent_units"][0]["recall_mode"] = "skip"
        social["intent_units"][0]["recall_queries"] = []
        social["episode_actions"] = [social["episode_actions"][0]]
        social["episode_links"] = []
        social["handoff"]["execution_mode"] = "respond"
        parsed = parse_context_plan(social, ["event-1"], [], "turn-1", 1)
        self.assertEqual(parsed["intent_units"][0]["recall_queries"], [])
        self.assertEqual(
            parsed["intent_units"][0]["recall"],
            {
                "mode": "skip",
                "reason": "no_unsupplied_history_dependency",
            },
        )

        invalid_skip_query = json.loads(json.dumps(social, ensure_ascii=False))
        invalid_skip_query["intent_units"][0]["recall_queries"] = ["不该搜索"]
        with self.assertRaisesRegex(ContextPlanError, "invalid_recall_skip"):
            parse_context_plan(invalid_skip_query, ["event-1"], [], "turn-1", 1)

        for speech_act in ("question", "correction", "request"):
            with self.subTest(speech_act=speech_act):
                grounded_work = json.loads(json.dumps(social, ensure_ascii=False))
                grounded_work["handoff"]["execution_mode"] = "work"
                grounded_work["intent_units"][0]["speech_act"] = speech_act
                parsed = parse_context_plan(
                    grounded_work,
                    ["event-1"],
                    [],
                    "turn-1",
                    1,
                )
                self.assertEqual(
                    parsed["intent_units"][0]["recall"]["mode"], "skip"
                )

        missing_context = json.loads(json.dumps(social, ensure_ascii=False))
        missing_context["handoff"]["context_status"] = "lookup_required"
        missing_context["handoff"]["context_needs"] = [
            {
                "tool": "conversation_search",
                "query": "旧约定",
                "evidence": "relevant_history",
            }
        ]
        with self.assertRaisesRegex(ContextPlanError, "invalid_recall_skip"):
            parse_context_plan(missing_context, ["event-1"], [], "turn-1", 1)

    def test_parser_validates_delivery_plan(self) -> None:
        plan = response_plan()
        parsed = parse_context_plan(plan, ["event-1"], [], "turn-1", 1)
        self.assertEqual(
            parsed["owner_handoff"]["execution"]["delivery"],
            {
                "mode": "bubbles",
                "bubbles": [
                    {
                        "timing": "after the requested work",
                        "form": "complete",
                        "purpose": "report the verified result",
                    }
                ],
            },
        )

        silent = response_plan()
        silent["handoff"]["delivery_mode"] = "silent"
        silent["handoff"]["delivery_bubbles"] = []
        silent["handoff"]["execution_reason"] = (
            "the closing beat needs no further acknowledgment"
        )
        parsed = parse_context_plan(silent, ["event-1"], [], "turn-1", 1)
        self.assertEqual(
            parsed["owner_handoff"]["execution"]["delivery"]["mode"],
            "silent",
        )

        social = response_plan()
        social["handoff"]["execution_mode"] = "respond"
        social["handoff"]["execution_outline"] = []
        social["handoff"]["execution_reason"] = "only visible social delivery is needed"
        social["handoff"]["delivery_bubbles"] = [
            {
                "timing": "immediately",
                "form": "fragmentary",
                "purpose": "standalone fragmentary social reaction",
            }
        ]
        parsed = parse_context_plan(social, ["event-1"], [], "turn-1", 1)
        self.assertEqual(parsed["owner_handoff"]["execution"]["outline"], [])
        self.assertEqual(
            parsed["owner_handoff"]["execution"]["delivery"]["bubbles"][0]["form"],
            "fragmentary",
        )

        invalid = response_plan()
        invalid["handoff"]["delivery_bubbles"] = [
            {"timing": "now", "form": "complete", "purpose": ""}
        ]
        with self.assertRaisesRegex(ContextPlanError, "invalid_delivery_purpose"):
            parse_context_plan(invalid, ["event-1"], [], "turn-1", 1)

        invalid_form = response_plan()
        invalid_form["handoff"]["delivery_bubbles"] = [
            {
                "timing": "now",
                "form": "short",
                "purpose": "react to the current moment",
            }
        ]
        with self.assertRaisesRegex(ContextPlanError, "invalid_delivery_form"):
            parse_context_plan(invalid_form, ["event-1"], [], "turn-1", 1)

        missing_form = response_plan()
        del missing_form["handoff"]["delivery_bubbles"][0]["form"]
        with self.assertRaisesRegex(ContextPlanError, "invalid_delivery_bubble"):
            parse_context_plan(missing_form, ["event-1"], [], "turn-1", 1)

        missing = response_plan()
        del missing["handoff"]["delivery_mode"]
        with self.assertRaisesRegex(ContextPlanError, "invalid_owner_handoff"):
            parse_context_plan(missing, ["event-1"], [], "turn-1", 1)

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
        self.assertEqual(parsed["episode_links"][0]["to_episode_id"], "older-game")

        plan["episode_links"][0]["to_episode_ref"] = "not-a-candidate"
        with self.assertRaisesRegex(ContextPlanError, "unknown_link_episode"):
            parse_context_plan(
                plan,
                ["event-1"],
                [{"id": "older-game"}],
                "turn-1",
                1,
            )

    def test_link_source_must_be_bound_by_current_turn(self) -> None:
        plan = response_plan()
        plan["episode_links"] = [
            {
                "from_episode_ref": "older-game",
                "to_episode_ref": "new:mail",
                "kind": "references",
            }
        ]
        with self.assertRaisesRegex(ContextPlanError, "invalid_episode_link"):
            parse_context_plan(
                plan,
                ["event-1"],
                [{"id": "older-game"}],
                "turn-1",
                1,
            )

    def test_links_reject_conflicting_kinds_and_ordering_cycles(self) -> None:
        conflicting = response_plan()
        conflicting["episode_links"] = [
            {
                "from_episode_ref": "new:mail",
                "to_episode_ref": "new:social",
                "kind": "references",
            },
            {
                "from_episode_ref": "new:mail",
                "to_episode_ref": "new:social",
                "kind": "supersedes",
            },
        ]
        with self.assertRaisesRegex(ContextPlanError, "conflicting_episode_link"):
            parse_context_plan(conflicting, ["event-1"], [], "turn-1", 1)

        cyclic = response_plan()
        cyclic["episode_links"] = [
            {
                "from_episode_ref": "new:mail",
                "to_episode_ref": "new:social",
                "kind": "continues",
            },
            {
                "from_episode_ref": "new:social",
                "to_episode_ref": "new:mail",
                "kind": "supersedes",
            },
        ]
        with self.assertRaisesRegex(ContextPlanError, "cyclic_episode_link"):
            parse_context_plan(cyclic, ["event-1"], [], "turn-1", 1)

    def test_new_episode_refs_require_ascii_slugs(self) -> None:
        plan = response_plan()
        plan["episode_actions"][0]["episode_ref"] = "new:一起玩游戏"
        with self.assertRaisesRegex(ContextPlanError, "invalid_new_episode_ref"):
            parse_context_plan(plan, ["event-1"], [], "turn-1", 1)

    def test_v3_defaults_optional_episode_metadata_and_keeps_existing_title(
        self,
    ) -> None:
        plan = response_plan()
        plan["episode_actions"] = [
            {"action": "none", "unit_ids": ["social"]},
            {
                "action": "continue",
                "episode_ref": "mail-thread",
                "title": "模型重复提供的标题会被忽略",
                "unit_ids": ["mail"],
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
        self.assertEqual(parsed["episode_actions"][1]["title"], "已有邮件话题")
        self.assertEqual(parsed["episode_actions"][1]["topics"], [])
        self.assertEqual(parsed["episode_actions"][1]["salience"], 0.0)

        missing_new_title = response_plan()
        del missing_new_title["episode_actions"][0]["title"]
        with self.assertRaisesRegex(ContextPlanError, "invalid_episode_action"):
            parse_context_plan(missing_new_title, ["event-1"], [], "turn-1", 1)

    def test_v3_requires_each_unit_exactly_once(self) -> None:
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

    def test_every_unit_requires_recall_decision(self) -> None:
        plan = response_plan()
        del plan["intent_units"][0]["recall_queries"]
        plan["episode_actions"][0]["open_loops"] = ["饭后再弄"]
        with self.assertRaisesRegex(ContextPlanError, "invalid_intent_unit"):
            parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)

    def test_relevant_history_is_valid_context_evidence(self) -> None:
        plan = response_plan()
        plan["handoff"]["context_status"] = "lookup_required"
        plan["handoff"]["context_needs"] = [
            {
                "tool": "memory_search",
                "query": "旧项目名称",
                "evidence": "relevant_history",
            }
        ]
        plan["handoff"]["context_reason"] = "需要相关历史"
        parsed = parse_context_plan(plan, ["event-1"], [], "turn-1", 1)
        self.assertEqual(
            parsed["owner_handoff"]["context"]["needs"][0]["evidence"],
            "relevant_history",
        )

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
                        "recall_queries": ["键盘"],
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
            recalled = build_plan_retrieval(daemon.store, plan, app_config(directory))
            self.assertEqual(
                [item["episode_id"] for item in recalled["episodes"]],
                ["hhkb"],
            )
            resident = [spec["name"] for spec in daemon._owner_tool_specs(plan)]
            self.assertEqual(
                resident,
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
                resident,
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
                resident,
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
        self.assertTrue(
            all(not item["recall_queries"] for item in plan["intent_units"])
        )
        self.assertIn("invalid_json", plan["uncertainty"][0])

    def test_planner_contract_and_internal_tool_catalog_stay_complete(self) -> None:
        compact_prompt = " ".join(CONTEXT_PLANNER_PROTOCOL_PROMPT.split())
        for phrase in (
            "final operative unit",
            "Every supplied event id",
            "globally bounded subset",
            "A need records missing evidence",
            "Use `clarify` only",
            "`episode_links` is empty by default",
            "source must be an Episode bound by this Turn",
            "Only events inside `<owner_messages>` are authenticated",
        ):
            self.assertIn(phrase, compact_prompt)
        self.assertNotIn(
            "Momoi's private Context Planner", CONTEXT_PLANNER_PROTOCOL_PROMPT
        )
        expected = {
            str(spec["name"])
            for spec in (*MEMORY_TOOL_SPECS, *AGENDA_TOOL_SPECS, *BUILTIN_TOOL_SPECS)
        }
        self.assertEqual(
            {item["id"] for item in PLANNER_INTERNAL_TOOLS},
            expected,
        )

    def test_planner_receives_rendered_downstream_system_contract(self) -> None:
        self.assertIn(
            DOWNSTREAM_OWNER_CONTRACT_PROMPT.replace(
                "{{STYLE_CARD}}", STYLE_CARD_SYSTEM_PROMPT
            ),
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn(
            "not your identity, tool protocol, or permission to act",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("## 8. Owner Turn output protocol", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn(
            "Each item is one non-empty private-chat bubble",
            CONTEXT_PLANNER_SYSTEM_PROMPT,
        )
        self.assertIn("{{SOUL}}", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn("{{STYLE_CARD}}", CONTEXT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("begin by simply speaking", CONTEXT_PLANNER_SYSTEM_PROMPT)
        for phrase in (
            "impulse genuinely arrives that way",
            "whole bubble",
            "completed content",
            "wordless reaction",
            "before, between, or after",
            "fixed default, minimum, preferred count",
        ):
            self.assertNotIn(phrase, CONTEXT_PLANNER_PROTOCOL_PROMPT)
            self.assertIn(phrase, STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn(
            "leave a planned fragment unfinished", CONTEXT_PLANNER_SYSTEM_PROMPT
        )
        self.assertNotIn("Momoi", STYLE_CARD_SYSTEM_PROMPT)
        schema = CONTEXT_PLAN_TOOL_SPEC["input_schema"]
        handoff = schema["properties"]["handoff"]  # type: ignore[index]
        self.assertNotIn("execution", handoff["properties"])
        self.assertIn("delivery_mode", handoff["required"])
        self.assertIn("delivery_bubbles", handoff["required"])
        self.assertNotIn("minItems", handoff["properties"]["execution_outline"])
        bubble = handoff["properties"]["delivery_bubbles"]["items"]
        self.assertEqual(set(bubble["required"]), {"timing", "form", "purpose"})
        self.assertEqual(
            bubble["properties"]["form"]["enum"],
            ["non_propositional", "fragmentary", "complete"],
        )
        self.assertNotIn("description", bubble["properties"]["form"])
        self.assertNotIn("description", bubble["properties"]["purpose"])

    def test_shared_owner_rules_are_not_duplicated_in_planner_protocol(self) -> None:
        for phrase in (
            "`delivery=uncertain`",
            "`reply_wait` records",
            "`plan_adjustment`: include it",
            "An open reconciliation means",
            "Current self state` is persistent mood",
        ):
            self.assertNotIn(phrase, CONTEXT_PLANNER_PROTOCOL_PROMPT)
            self.assertIn(phrase, DOWNSTREAM_OWNER_CONTRACT_PROMPT)


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
                        "version": 3,
                        "intent_units": [
                            {
                                "id": "u1",
                                "event_ids": ["event-1", "event-2"],
                                "text": "第一条；第二条",
                                "intent": "combined owner update",
                                "speech_act": "casual_share",
                                "references": [],
                                "recall_mode": "search",
                                "recall_queries": ["第一条 | 第二条"],
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
                        "handoff": {
                            "context_status": "sufficient",
                            "context_needs": [],
                            "context_reason": "当前上下文足够",
                            "mcp_servers": [],
                            "mcp_reason": "不需要外部服务",
                            "execution_mode": "respond",
                            "execution_outline": ["处理合并后的主人消息"],
                            "execution_reason": "当前输入已足够回应",
                            "delivery_mode": "bubbles",
                            "delivery_bubbles": [
                                {
                                    "timing": "immediately",
                                    "form": "complete",
                                    "purpose": "respond to the combined owner update",
                                }
                            ],
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
                        "version": 3,
                        "intent_units": [
                            {
                                "id": "u1",
                                "event_ids": ["semantic-event"],
                                "text": "我把喝水用的东西搁哪儿啦",
                                "intent": "find stored drinking container",
                                "speech_act": "question",
                                "references": ["喝水用的东西"],
                                "recall_mode": "search",
                                "recall_queries": ["保温杯 | 收纳位置"],
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
                        "handoff": {
                            "context_status": "lookup_required",
                            "context_needs": [
                                {
                                    "tool": "conversation_search",
                                    "query": "保温杯 阁楼 收纳位置",
                                    "evidence": "unresolved_reference",
                                }
                            ],
                            "context_reason": "需要查历史收纳位置",
                            "mcp_servers": [],
                            "mcp_reason": "不需要外部服务",
                            "execution_mode": "respond",
                            "execution_outline": ["查找收纳位置", "根据证据回答"],
                            "execution_reason": "主人在问旧物品位置",
                            "delivery_mode": "bubbles",
                            "delivery_bubbles": [
                                {
                                    "timing": "after resolving the stored location",
                                    "form": "complete",
                                    "purpose": "answer with the recalled location",
                                }
                            ],
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
                        (
                            "fixed.style",
                            "长期记忆：喜欢简短回复",
                            "always",
                            memory_now,
                            memory_now,
                        ),
                        (
                            "recent.mail",
                            "近期记忆：正在等待邮件",
                            "recent",
                            memory_now,
                            memory_now,
                        ),
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
                                "available_internal_tools",
                                "available_mcp_servers",
                                "long_term_memories",
                                "recent_memories",
                                "candidate_goals",
                                "candidate_reminders",
                                "interrupted_reply_expectation",
                                "recent_turn_base",
                                "recent_turn_append",
                                "recent_turn_focus",
                                "candidate_episodes",
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
                        recent = (
                            str(payload["recent_turn_base"])
                            + "\n"
                            + str(payload["recent_turn_append"])
                        )
                        provider_self.planner_recent = recent
                        self.assertIn("RECENT CONTEXT 1", recent)
                        self.assertIn("RECENT CONTEXT 2", recent)
                        self.assertIn("GLOBAL RAW MUST NOT LEAK", recent)
                        self.assertNotIn(" active ", recent)
                        self.assertNotIn(" background ", recent)
                        self.assertEqual(
                            payload["interrupted_reply_expectation"], "(none)"
                        )
                        self.assertIn("喜欢简短回复", payload["long_term_memories"])
                        self.assertIn("正在等待邮件", payload["recent_memories"])
                        self.assertEqual(payload["candidate_goals"].count("id="), 8)
                        self.assertIn("id=goal-8", payload["candidate_goals"])
                        self.assertNotIn("id=goal-0", payload["candidate_goals"])
                        self.assertEqual(payload["candidate_reminders"].count("id="), 8)
                        self.assertIn("id=reminder-8", payload["candidate_reminders"])
                        self.assertNotIn(
                            "id=reminder-0", payload["candidate_reminders"]
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
                    self.assertIn("<recent_turn_base>", text)
                    self.assertIn("<recent_turn_append>", text)
                    self.assertNotIn("<recent_conversation>", text)
                    self.assertIn("<recall_status>", text)
                    self.assertIn("queries=微博 | 等待中的邮件", text)
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
                    self.assertNotIn('"salience"', text)
                    self.assertIn("RECENT CONTEXT 2", text)
                    self.assertIn("GLOBAL RAW MUST NOT LEAK", text)
                    self.assertEqual(len(messages), 1)
                    call = ToolCall(
                        "respond",
                        "respond",
                        {
                            "expects_reply": False,
                            "reply_expectation": "",
                            "mood": {"decision": "unchanged"},
                            "activity": {"decision": "unchanged"},
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
            self.assertIn("GLOBAL RAW MUST NOT LEAK", provider.main_rendered)
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
                            "activity": {"decision": "unchanged"},
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
