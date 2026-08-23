import json
import tempfile
import time
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, MemoryCandidate, TurnDraft
from momoi.runtime.context_assembler import (
    _episode_header,
    _planner_final,
    _rank_recall_items,
    _recalled_turn_context,
    assemble_main_context,
    assemble_planner_recent_turns,
    assemble_recent_turns,
    build_plan_retrieval,
    project_recent_turns_for_planner,
    project_recent_turns_for_owner,
    recall_episode_context,
    recall_episode_context_parts,
    render_planner_recent_turn_focus,
    render_planner_recent_turns,
)
from momoi.runtime.turn_support import pack_user_context
from momoi.storage import Store, estimate_tokens


def config(
    directory: str,
    memory_results: int = 6,
    recent_episode_hours: float = 6,
    summary_results: int = 3,
) -> AppConfig:
    return AppConfig(
        llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
        channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        system_prompt="test",
        recent_raw_tokens=2000,
        recent_turns=2,
        memory_results=memory_results,
        memory_tokens=4000,
        summary_results=summary_results,
        summary_tokens=2000,
        recent_episode_hours=recent_episode_hours,
        database=Path(directory) / "momoi.sqlite3",
        log_level="INFO",
    )


def plan(query: str, episode_id: str = "episode-mail") -> dict[str, object]:
    return {
        "version": 1,
        "intent_units": [
            {
                "id": "mail",
                "event_ids": ["current"],
                "text": "看看之前等的项目邮件",
                "intent": "check expected project email",
                "references": ["之前等的"],
                "recall_queries": [query],
            }
        ],
        "episode_actions": [
            {
                "action": "continue",
                "episode_id": episode_id,
                "is_new": False,
                "title": "项目邮件",
                "relation": "primary",
                "unit_ids": ["mail"],
                "topics": ["项目邮件"],
                "entities": [],
                "open_loops": [],
                "salience": 0.8,
            }
        ],
        "episode_links": [],
        "uncertainty": [],
    }


class ContextAssemblerTest(unittest.TestCase):
    def test_plan_recall_prioritizes_each_units_first_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            recall_plan = plan("a1")
            recall_plan["intent_units"] = [
                {
                    "id": unit_id,
                    "event_ids": ["current"],
                    "text": unit_id,
                    "intent": unit_id,
                    "references": [],
                    "recall_queries": [f"{unit_id}1", f"{unit_id}2", f"{unit_id}3"],
                }
                for unit_id in ("a", "b", "c")
            ]
            recall_plan["episode_actions"][0]["unit_ids"] = ["a", "b", "c"]

            retrieval = build_plan_retrieval(
                store,
                recall_plan,
                config(directory, recent_episode_hours=0),
            )

            self.assertIn(
                "queries=a1 | b1 | c1 | a2 | b2 | c2",
                retrieval["query_recall"],
            )
            self.assertIn("query_count=6/9", retrieval["query_recall"])
            store.close()

    def test_recall_ranking_prefers_recency_then_keyword_count(self) -> None:
        ranked = _rank_recall_items(
            [
                {
                    "turn_id": "keyword",
                    "matched_keywords": ["a"],
                    "last_activity_at": 50,
                },
                {
                    "turn_id": "recent",
                    "is_recent": True,
                    "last_activity_at": 50,
                },
                {
                    "turn_id": "multi",
                    "matched_keywords": ["a", "b"],
                    "last_activity_at": 50,
                },
                {
                    "turn_id": "recent-keyword",
                    "is_recent": True,
                    "matched_keywords": ["a"],
                    "last_activity_at": 50,
                },
                {
                    "turn_id": "recent-multi",
                    "is_recent": True,
                    "matched_keywords": ["a", "b"],
                    "last_activity_at": 50,
                },
            ]
        )

        self.assertEqual(
            [item["turn_id"] for item in ranked],
            [
                "recent-multi",
                "recent-keyword",
                "multi",
                "recent",
                "keyword",
            ],
        )

    def test_recalled_turn_context_has_independent_six_thousand_token_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            turns = []
            for index in range(8):
                event = IncomingMessage(
                    f"recall-budget-{index}",
                    f"recall-budget-{index}",
                    f"关键词{index} " + "很长的主人聊天内容" * 500,
                    index + 1,
                    index + 1,
                )
                store.add_event(event)
                store.commit_turn(
                    [event],
                    event.text,
                    AgentReply([]),
                    turn_id=f"recall-budget-turn-{index}",
                )
                turns.append(
                    {
                        "turn_id": f"recall-budget-turn-{index}",
                        "matched_keywords": [f"关键词{index}"],
                        "last_activity_at": index,
                    }
                )

            rendered = _recalled_turn_context(store, turns, 6000)

            self.assertLessEqual(estimate_tokens(rendered), 6000)
            self.assertLessEqual(rendered.count("[recalled turn="), 6)
            self.assertGreater(rendered.count("[recalled turn="), 0)
            store.close()

    def test_recent_turns_keep_six_turns_without_token_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            for index in range(8):
                event = IncomingMessage(
                    f"recent-full-{index}",
                    f"recent-full-{index}",
                    f"第{index}轮 " + "包含较长内容" * 500,
                    index + 1,
                    index + 1,
                )
                store.add_event(event)
                store.commit_turn(
                    [event],
                    event.text,
                    AgentReply([]),
                    turn_id=f"recent-full-turn-{index}",
                )

            document, _ = assemble_recent_turns(store, 6, None)
            rendered = project_recent_turns_for_owner(document, None)

            self.assertEqual(len(document["turns"]), 6)
            self.assertEqual(rendered.count("\n\nT-") + 1, 6)
            self.assertIn("第2轮", rendered)
            self.assertIn("第7轮", rendered)
            store.close()

    def test_main_recent_turns_keep_stable_base_and_dynamic_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")

            def add(index: int) -> None:
                event = IncomingMessage(
                    f"event-{index}",
                    f"event-{index}",
                    f"第{index}轮",
                    index,
                    index,
                )
                store.add_event(event)
                store.commit_turn(
                    [event],
                    event.text,
                    AgentReply([f"回复{index}"]),
                    turn_id=f"turn-{index}",
                )

            for index in range(1, 7):
                add(index)
            six = assemble_main_context(store, {}, 2000, 2000, recent_turns=6)

            add(7)
            seven = assemble_main_context(store, {}, 2000, 2000, recent_turns=6)

            self.assertEqual(six["recent_turn_base"], seven["recent_turn_base"])
            self.assertEqual(six["recent_turn_append"], "")
            self.assertIn("T-7", seven["recent_turn_append"])
            self.assertIn("第7轮", seven["recent_turn_append"])
            self.assertEqual(
                seven["recent_turns"],
                seven["recent_turn_base"] + "\n\n" + seven["recent_turn_append"],
            )

            for index in range(8, 12):
                add(index)
            eleven = assemble_main_context(store, {}, 2000, 2000, recent_turns=6)
            self.assertEqual(eleven["recent_turn_base"], six["recent_turn_base"])
            self.assertEqual(eleven["recent_turn_append"].count("\n\nT-") + 1, 5)
            self.assertIn("第11轮", eleven["recent_turn_append"])

            add(12)
            twelve = assemble_main_context(store, {}, 2000, 2000, recent_turns=6)
            self.assertEqual(twelve["recent_turn_append"], "")
            self.assertNotEqual(twelve["recent_turn_base"], six["recent_turn_base"])
            self.assertNotIn("第6轮", twelve["recent_turn_base"])
            self.assertIn("第7轮", twelve["recent_turn_base"])
            self.assertIn("第12轮", twelve["recent_turn_base"])

            add(13)
            thirteen = assemble_main_context(store, {}, 2000, 2000, recent_turns=6)
            self.assertEqual(thirteen["recent_turn_base"], twelve["recent_turn_base"])
            self.assertIn("T-7", thirteen["recent_turn_append"])
            self.assertIn("第13轮", thirteen["recent_turn_append"])
            store.close()

    def test_owner_projection_removes_legacy_channel_prefixes(self) -> None:
        rendered = project_recent_turns_for_owner(
            {
                "turns": [
                    {
                        "timeline": [
                            {
                                "type": "owner_message",
                                "text": "2026-08-23T22:50:28+08:00 [napcat] 在干嘛",
                            },
                            {
                                "type": "owner_message",
                                "text": "2026-08-23T22:51:28+08:00 [weixin] 回我一下",
                            },
                        ]
                    }
                ]
            },
            None,
        )

        self.assertIn("owner: 2026-08-23T22:50:28+08:00 在干嘛", rendered)
        self.assertIn("owner: 2026-08-23T22:51:28+08:00 回我一下", rendered)
        self.assertNotIn("[napcat]", rendered)
        self.assertNotIn("[weixin]", rendered)

    def test_owner_context_puts_fixed_memory_and_agenda_state_first(self) -> None:
        rendered = pack_user_context(
            ("recent_turn_append", "appended history"),
            ("recent_turn_base", "stable history"),
            ("pending_reminders", "reminders"),
            ("recent_memories", "recent"),
            ("active_goals", "goals"),
            ("long_term_memories", "long term"),
            ("recall_memories", "recalled"),
            ("episode_directory", "episodes"),
            ("recalled_turns", "recalled turns"),
            ("interrupted_reply_expectation", "interrupted"),
        )
        self.assertLess(rendered.index("<long_term_memories>"), rendered.index("<recent_memories>"))
        self.assertLess(rendered.index("<recent_memories>"), rendered.index("<active_goals>"))
        self.assertLess(rendered.index("<active_goals>"), rendered.index("<pending_reminders>"))
        self.assertLess(rendered.index("<pending_reminders>"), rendered.index("<interrupted_reply_expectation>"))
        self.assertLess(rendered.index("<interrupted_reply_expectation>"), rendered.index("<recent_turn_base>"))
        self.assertLess(rendered.index("<recent_turn_base>"), rendered.index("<recent_turn_append>"))
        self.assertLess(rendered.index("<recent_turn_append>"), rendered.index("<recall_memories>"))
        self.assertLess(rendered.index("<recall_memories>"), rendered.index("<episode_directory>"))
        self.assertLess(rendered.index("<episode_directory>"), rendered.index("<recalled_turns>"))

    def test_retrieval_keeps_fixed_and_dynamic_memory_layers_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.executemany(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('profile', ?, ?, ?, 'owner', 'source', 'evidence',
                               0.8, ?, ?)""",
                    [
                        ("fixed.cup", "长期记得蓝色杯子", "always", now, now),
                        ("recalled.cup", "召回蓝色杯子的旧位置", "recall", now, now),
                    ],
                )
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:layers', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.executemany(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES (?, ?, ?, 'evidence', 0.8,
                               'reflection:layers', ?, ?)""",
                    [
                        ("owner_profile", "cup.core", "复盘认为蓝色杯子很重要", now, now),
                        ("shared_experience", "cup.day", "那天一起找过蓝色杯子", now, now),
                        ("owner_profile", "cat.unrelated", "复盘认为主人喜欢猫", now, now),
                    ],
                )

            retrieval = build_plan_retrieval(
                store, plan("蓝色杯子"), config(directory)
            )

            self.assertIn("长期记得蓝色杯子", retrieval["long_term_memories"])
            recalled = "\n".join(
                str(item["content"]) for item in retrieval["recall_memories"]
            )
            self.assertIn("召回蓝色杯子的旧位置", recalled)
            self.assertNotIn("长期记得蓝色杯子", recalled)
            reflections = "\n".join(
                str(item["content"]) for item in retrieval["reflection_memories"]
            )
            self.assertIn("复盘认为蓝色杯子很重要", reflections)
            self.assertIn("那天一起找过蓝色杯子", reflections)
            self.assertNotIn("复盘认为主人喜欢猫", reflections)

            heartbeat_retrieval = build_plan_retrieval(
                store,
                {
                    "activity": {
                        "intent": "整理蓝色杯子的共同回忆",
                        "reason": "想回顾这件事",
                        "recall_queries": ["蓝色杯子"],
                    }
                },
                config(directory),
            )
            self.assertTrue(heartbeat_retrieval["recall_memories"])
            self.assertTrue(heartbeat_retrieval["reflection_memories"])
            self.assertIn("queries=蓝色杯子", heartbeat_retrieval["query_recall"])
            store.close()

    def test_owner_history_keeps_action_ledger_for_memory_and_external_tools(self) -> None:
        rendered = project_recent_turns_for_owner(
            {
                "turns": [
                    {
                        "started_at": "2026-08-20T10:00:00+08:00",
                        "timeline": [
                            {
                                "type": "tool_call",
                                "tool_call_id": "call-12345678",
                                "name": "memory_remember",
                                "arguments": {"kind": "shared", "key": "games", "content": "共同游玩"},
                            },
                            {
                                "type": "tool_result",
                                "tool_call_id": "call-12345678",
                                "result": {"state": "staged", "memory": {"kind": "shared", "key": "games", "activation": "recall"}},
                            },
                            {
                                "type": "tool_call",
                                "tool_call_id": "call-87654321",
                                "name": "mcp__social__feed",
                                "arguments": {"query": "游戏"},
                            },
                            {
                                "type": "tool_result",
                                "tool_call_id": "call-87654321",
                                "result": {"result": "feed fetched: 3 relevant posts"},
                            },
                        ],
                        "final": {"mutations": {"memories": [{"kind": "shared", "key": "games"}]}},
                    }
                ]
            },
            500,
        )
        self.assertIn("memory_remember", rendered)
        self.assertIn("shared:games", rendered)
        self.assertIn("feed fetched", rendered)
        self.assertIn("final: memories=shared:games", rendered)

    def test_planner_recent_turns_mark_internal_records_apart_from_speech(
        self,
    ) -> None:
        rendered = render_planner_recent_turns(
            {
                "version": 1,
                "turns": [
                    {
                        "at": "2026-08-23T07:30:00+08:00",
                        "kind": "autonomous",
                        "timeline": [
                            {
                                "type": "assistant_message",
                                "text": "[AUTONOMOUS GOAL REVIEW RECORD]\nLatest result: 已发送建议",
                                "delivery": "internal",
                            },
                            {
                                "type": "assistant_message",
                                "text": "周日早安！南岸今天多云",
                            },
                        ],
                    }
                ],
            }
        )
        self.assertIn("momoi [internal]: [AUTONOMOUS GOAL REVIEW RECORD]", rendered)
        self.assertIn("momoi: 周日早安！南岸今天多云", rendered)

    def test_planner_projection_preserves_owner_plan_adjustment(self) -> None:
        adjustment = {
            "reason": "工具证据推翻旧引用",
            "corrected_direction": "改为处理当前任务",
            "resolved_context_needs": ["conversation_search"],
        }
        self.assertEqual(
            _planner_final({"plan_adjustment": adjustment}),
            {"plan_adjustment": adjustment},
        )

    def test_planner_recent_turns_use_six_plus_six_cache_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")

            def add(index: int) -> None:
                event = IncomingMessage(
                    f"event-{index}",
                    f"event-{index}",
                    f"owner-{index}",
                    index,
                    index,
                )
                store.add_event(event)
                store.begin_turn(f"turn-{index}", "owner", [event.event_id])
                if index == 1:
                    store.append_turn_journal(
                        f"turn-{index}",
                        "tool_call",
                        {
                            "tool_call_id": "gmail",
                            "name": "mcp__gog__gmail_search",
                            "arguments": {"query": "newer_than:1d"},
                        },
                    )
                    store.append_turn_journal(
                        f"turn-{index}",
                        "tool_result",
                        {
                            "tool_call_id": "gmail",
                            "name": "mcp__gog__gmail_search",
                            "ok": True,
                            "error": None,
                            "result": {
                                "ok": True,
                                "result": {
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "邮件结果" * 2000,
                                        }
                                    ]
                                },
                            },
                        },
                    )
                store.commit_turn(
                    [event],
                    event.text,
                    AgentReply([f"assistant-{index}"]),
                    turn_id=f"turn-{index}",
                )
                outbox_id = store._db.execute(
                    "SELECT id FROM outbox WHERE turn_id=?",
                    (f"turn-{index}",),
                ).fetchone()["id"]
                store.mark_sent(int(outbox_id))

            for index in range(1, 7):
                add(index)
            six, active, base_count = assemble_planner_recent_turns(
                store, 6, 6, 6, 88000
            )
            self.assertEqual(
                [turn["turn_id"] for turn in six["turns"]],
                [f"turn-{index}" for index in range(1, 7)],
            )
            self.assertEqual(active, [f"turn-{index}" for index in range(1, 7)])
            self.assertEqual(base_count, 6)
            first_result = next(
                item
                for item in six["turns"][0]["timeline"]
                if item["type"] == "tool_result"
            )
            self.assertTrue(first_result["result"]["result"]["truncated"])

            add(7)
            seven, active, base_count = assemble_planner_recent_turns(
                store, 6, 6, 6, 88000
            )
            self.assertEqual(
                [turn["turn_id"] for turn in seven["turns"]],
                [f"turn-{index}" for index in range(1, 8)],
            )
            self.assertEqual(active, [f"turn-{index}" for index in range(2, 8)])
            self.assertEqual(base_count, 6)
            six_base = render_planner_recent_turns(
                {"version": 1, "turns": six["turns"][:6]}
            )
            seven_base = render_planner_recent_turns(
                {"version": 1, "turns": seven["turns"][:base_count]}
            )
            self.assertEqual(six_base, seven_base)
            self.assertNotIn(" active ", seven_base)
            self.assertNotIn(" background ", seven_base)
            self.assertEqual(
                render_planner_recent_turn_focus(seven, active),
                "T-2, T-3, T-4, T-5, T-6, T-7",
            )
            six_prefix = json.dumps(
                six, ensure_ascii=False, separators=(",", ":")
            )[:-2]
            self.assertTrue(
                json.dumps(
                    seven, ensure_ascii=False, separators=(",", ":")
                ).startswith(six_prefix)
            )

            for index in range(8, 12):
                add(index)
            eleven, active, base_count = assemble_planner_recent_turns(
                store, 6, 6, 6, 88000
            )
            self.assertEqual(len(eleven["turns"]), 11)
            self.assertEqual(active, [f"turn-{index}" for index in range(6, 12)])
            self.assertEqual(base_count, 6)
            self.assertTrue(
                json.dumps(
                    eleven, ensure_ascii=False, separators=(",", ":")
                ).startswith(six_prefix)
            )

            add(12)
            twelve, active, base_count = assemble_planner_recent_turns(
                store, 6, 6, 6, 88000
            )
            self.assertEqual(
                [turn["turn_id"] for turn in twelve["turns"]],
                [f"turn-{index}" for index in range(7, 13)],
            )
            self.assertEqual(active, [f"turn-{index}" for index in range(7, 13)])
            self.assertEqual(base_count, 6)
            store.close()

    def test_planner_recent_turns_drop_previous_block_at_token_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            for index in range(1, 8):
                event = IncomingMessage(
                    f"event-{index}",
                    f"event-{index}",
                    f"owner-{index}-" + ("x" * 1000),
                    index,
                    index,
                )
                store.add_event(event)
                store.begin_turn(f"turn-{index}", "owner", [event.event_id])
                store.commit_turn(
                    [event],
                    event.text,
                    AgentReply([f"assistant-{index}"]),
                    turn_id=f"turn-{index}",
                )

            one, _, _ = assemble_planner_recent_turns(
                store, 1, 1, 1, 88000
            )
            one_size = estimate_tokens(
                json.dumps(one, ensure_ascii=False, separators=(",", ":"))
            )
            selected, active, base_count = assemble_planner_recent_turns(
                store,
                6,
                6,
                6,
                one_size + 10,
            )
            self.assertEqual(
                [turn["turn_id"] for turn in selected["turns"]],
                ["turn-7"],
            )
            self.assertEqual(active, ["turn-7"])
            self.assertEqual(base_count, 0)
            store.close()

    def test_recent_turns_keep_messages_tools_and_committed_mutations_together(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage(
                "owner-correction",
                "owner-correction",
                "这是个双关",
                1,
                1,
            )
            store.add_event(event)
            store.begin_turn("turn-correction", "owner", [event.event_id])
            store.save_context_plan(
                "turn-correction",
                1,
                [event.event_id],
                {
                    "version": 2,
                    "intent_units": [
                        {
                            "id": "u1",
                            "event_ids": [event.event_id],
                            "text": event.text,
                            "intent": "纠正网络梗理解",
                            "speech_act": "correction",
                            "references": [],
                            "recall_queries": [],
                        }
                    ],
                    "episode_actions": [
                        {"action": "none", "unit_ids": ["u1"]}
                    ],
                    "episode_links": [],
                    "uncertainty": [],
                },
            )
            store.append_turn_journal(
                "turn-correction",
                "tool_call",
                {
                    "tool_call_id": "remember",
                    "name": "memory_remember",
                    "source": "memory",
                    "arguments": {
                        "kind": "shared",
                        "key": "meme.example",
                        "content": "一个待更正的解释",
                        "evidence": "这是个双关",
                    },
                },
            )
            store.append_turn_journal(
                "turn-correction",
                "tool_result",
                {
                    "tool_call_id": "remember",
                    "name": "memory_remember",
                    "ok": True,
                    "error": None,
                    "result": {"ok": True, "state": "staged"},
                },
            )
            draft = TurnDraft(
                memories=[
                    MemoryCandidate(
                        "shared",
                        "meme.example",
                        "一个待更正的解释",
                        "这是个双关",
                        activation="recall",
                    )
                ]
            )
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["我先这样理解"]),
                draft,
                turn_id="turn-correction",
            )

            document, rendered = assemble_recent_turns(store, 2, 4000)

            records = document["turns"]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["turn_id"], "turn-correction")
            self.assertEqual(
                record["interpretation"]["intents"][0]["speech_act"],
                "correction",
            )
            types = [item["type"] for item in record["timeline"]]
            self.assertIn("owner_message", types)
            self.assertIn("tool_call", types)
            self.assertIn("tool_result", types)
            self.assertIn("assistant_message", types)
            self.assertEqual(
                record["final"]["mutations"]["memories"][0]["key"],
                "meme.example",
            )
            parsed = json.loads(rendered)
            self.assertEqual(parsed["version"], 1)
            self.assertEqual(parsed["turns"][0]["turn_id"], "turn-correction")
            self.assertIn("meme.example", rendered)

            projected = project_recent_turns_for_planner(document)
            projected_record = projected["turns"][0]
            projected_timeline = projected_record["timeline"]
            tool_call = next(
                item for item in projected_timeline if item["type"] == "tool_call"
            )
            tool_result = next(
                item for item in projected_timeline if item["type"] == "tool_result"
            )
            self.assertEqual(tool_call["call"], "t1")
            self.assertEqual(tool_result["call"], "t1")
            self.assertEqual(tool_call["name"], "memory_remember")
            self.assertEqual(
                tool_call["arguments"],
                {
                    "kind": "shared",
                    "key": "meme.example",
                    "content": "一个待更正的解释",
                    "evidence": "这是个双关",
                },
            )
            self.assertEqual(tool_result["result"], {"state": "staged"})
            self.assertNotIn("ok", tool_result)
            self.assertNotIn("error", tool_result)
            self.assertNotIn("name", tool_result)
            self.assertNotIn("visibility", tool_result)
            self.assertNotIn("timestamp", tool_result)
            self.assertNotIn("tool_call_id", tool_call)
            self.assertNotIn("source", tool_call)
            self.assertNotIn("trust", tool_result)
            self.assertNotIn("llm", projected_record["final"])
            self.assertNotIn("kind", projected_record)
            self.assertNotIn("state", projected_record)
            self.assertNotIn("completed_at", projected_record)
            self.assertIn("at", projected_record)
            projected_intent = projected_record["interpretation"]["intents"][0]
            self.assertNotIn("id", projected_intent)
            self.assertNotIn("text", projected_intent)
            self.assertEqual(projected_intent["speech_act"], "correction")
            self.assertEqual(
                projected_record["interpretation"]["episode_actions"][0][
                    "intent_indexes"
                ],
                [0],
            )
            self.assertNotIn(
                "uncertainty", projected_record["interpretation"]
            )
            owner_message = next(
                item
                for item in projected_timeline
                if item["type"] == "owner_message"
            )
            self.assertEqual(owner_message["text"], "这是个双关")
            self.assertNotIn("trust", owner_message)
            self.assertNotIn("delivery", owner_message)
            self.assertNotIn("timestamp", owner_message)
            self.assertEqual(
                next(
                    item
                    for item in document["turns"][0]["timeline"]
                    if item["type"] == "tool_call"
                )["tool_call_id"],
                "remember",
            )
            store.close()

    def test_planner_projection_omits_defaults_but_keeps_exceptions(self) -> None:
        projected = project_recent_turns_for_planner(
            {
                "version": 1,
                "turns": [
                    {
                        "turn_id": "turn-1",
                        "kind": "owner",
                        "state": "completed",
                        "channel": "napcat",
                        "started_at": "2026-08-19T07:34:03+08:00",
                        "completed_at": "2026-08-19T07:34:37+08:00",
                        "interpretation": {
                            "intents": [],
                            "episode_actions": [],
                            "uncertainty": [],
                        },
                        "final": {
                            "external_effect": False,
                            "failure": "",
                            "reply_wait": {"wait": False},
                            "mood_change": None,
                            "mutations": {
                                "memories": [],
                                "goals": [],
                            },
                        },
                        "timeline": [
                            {
                                "type": "owner_message",
                                "timestamp": "2026-08-19T07:34:03+08:00",
                                "text": (
                                    "2026-08-19T07:34:03+08:00 "
                                    "[napcat] 抱抱"
                                ),
                                "delivery": "delivered",
                                "trust": "owner",
                            },
                            {
                                "type": "assistant_message",
                                "timestamp": "2026-08-19T07:34:37+08:00",
                                "text": "抱紧啦",
                                "delivery": "uncertain",
                                "trust": "context_data",
                            },
                            {
                                "type": "tool_result",
                                "call": "t1",
                                "name": "example",
                                "ok": True,
                                "error": None,
                                "result": {"value": 1},
                                "timestamp": "2026-08-19T07:34:20+08:00",
                                "visibility": "internal",
                            },
                        ],
                    }
                ],
            }
        )
        turn = projected["turns"][0]
        self.assertEqual(turn["at"], "2026-08-19T07:34:03+08:00")
        self.assertNotIn("channel", turn)
        self.assertNotIn("final", turn)
        self.assertNotIn("interpretation", turn)
        self.assertEqual(
            turn["timeline"][0],
            {"type": "owner_message", "text": "抱抱"},
        )
        self.assertEqual(
            turn["timeline"][1],
            {
                "type": "assistant_message",
                "text": "抱紧啦",
                "delivery": "uncertain",
            },
        )
        self.assertEqual(turn["timeline"][2]["result"], {"value": 1})
        self.assertNotIn("visibility", turn["timeline"][2])
        self.assertNotIn("timestamp", turn["timeline"][2])

    def test_episode_header_omits_directory_defaults(self) -> None:
        episode = {
            "id": "ep-1",
            "status": "open",
            "created_timestamp": "2026-08-16T12:00:55+08:00",
            "updated_timestamp": "2026-08-20T15:04:55+08:00",
        }
        self.assertEqual(
            _episode_header(episode, {"relation": "recent", "unit_ids": []}),
            "[episode id=ep-1]",
        )
        self.assertEqual(
            _episode_header(
                {**episode, "status": "closed"},
                {"relation": "recalled", "unit_ids": ["unit-1"]},
            ),
            "[episode id=ep-1 units=unit-1 relation=recalled status=closed]",
        )

    def test_planner_projection_compacts_historical_tool_results_uniformly(
        self,
    ) -> None:
        large_content = "结果内容" * 1200
        entries = [f"file-{index}.txt" for index in range(100)]
        document = {
            "version": 1,
            "turns": [
                {
                    "turn_id": "background",
                    "timeline": [
                        {
                            "type": "tool_call",
                            "tool_call_id": "read",
                            "name": "read_file",
                            "arguments": {"path": "/tmp/example.txt"},
                        },
                        {
                            "type": "tool_result",
                            "tool_call_id": "read",
                            "name": "read_file",
                            "ok": True,
                            "error": None,
                            "result": {
                                "ok": True,
                                "error": None,
                                "truncated": False,
                                "provenance": {
                                    "source": "builtin",
                                    "tool": "read_file",
                                },
                                "path": "/tmp/example.txt",
                                "content": large_content,
                            },
                        },
                        {
                            "type": "tool_call",
                            "tool_call_id": "list",
                            "name": "list_dir",
                            "arguments": {"path": "/tmp"},
                        },
                        {
                            "type": "tool_result",
                            "tool_call_id": "list",
                            "name": "list_dir",
                            "ok": True,
                            "error": None,
                            "result": {
                                "ok": True,
                                "error": None,
                                "truncated": False,
                                "provenance": {
                                    "source": "builtin",
                                    "tool": "list_dir",
                                },
                                "count": len(entries),
                                "entries": entries,
                            },
                        },
                        {
                            "type": "tool_call",
                            "tool_call_id": "goal",
                            "name": "goal_update",
                            "arguments": {
                                "goal_id": "goal-1",
                                "status": "waiting",
                                "waiting_for": "老师回复",
                            },
                        },
                        {
                            "type": "tool_result",
                            "tool_call_id": "goal",
                            "name": "goal_update",
                            "ok": True,
                            "error": None,
                            "result": {
                                "ok": True,
                                "state": "staged",
                                "goal": {
                                    "id": "goal-1",
                                    "title": "测试目标",
                                    "status": "waiting",
                                    "success_criteria": "不需要重复给Planner",
                                    "authority": "owner",
                                    "source_event_id": "event",
                                    "plan": ["第一步", "第二步"],
                                    "waiting_for": "老师回复",
                                    "next_action": "等待",
                                },
                            },
                        },
                        {
                            "type": "tool_call",
                            "tool_call_id": "failed",
                            "name": "curl",
                            "arguments": {"url": "https://example.com"},
                        },
                        {
                            "type": "tool_result",
                            "tool_call_id": "failed",
                            "name": "curl",
                            "ok": False,
                            "error": "timeout",
                            "result": {"ok": False, "error": "timeout"},
                        },
                    ],
                },
                {
                    "turn_id": "active",
                    "timeline": [
                        {
                            "type": "tool_call",
                            "tool_call_id": "active-read",
                            "name": "read_file",
                            "arguments": {"path": "/tmp/active.txt"},
                        },
                        {
                            "type": "tool_result",
                            "tool_call_id": "active-read",
                            "name": "read_file",
                            "ok": True,
                            "error": None,
                            "result": {
                                "ok": True,
                                "error": None,
                                "content": large_content,
                            },
                        },
                    ],
                },
            ],
        }
        full = project_recent_turns_for_planner(document)
        projected = project_recent_turns_for_planner(
            document,
            compact_tool_results=True,
        )
        compact_results = [
            item
            for item in projected["turns"][0]["timeline"]
            if item["type"] == "tool_result"
        ]
        self.assertTrue(
            compact_results[0]["result"]["content"]["truncated"]
        )
        self.assertEqual(
            compact_results[1]["result"]["entries"]["original_items"],
            100,
        )
        compact_goal = compact_results[2]["result"]["goal"]
        self.assertEqual(compact_goal["id"], "goal-1")
        self.assertEqual(compact_goal["waiting_for"], "老师回复")
        self.assertNotIn("success_criteria", compact_goal)
        self.assertNotIn("plan", compact_goal)
        self.assertEqual(compact_results[3]["error"], "timeout")

        active_result = projected["turns"][1]["timeline"][1]["result"]
        self.assertTrue(active_result["content"]["truncated"])
        full_active_result = full["turns"][1]["timeline"][1]["result"]
        self.assertEqual(full_active_result["content"], large_content)

    def test_planner_projection_caps_unknown_large_result_shapes(self) -> None:
        document = {
            "version": 1,
            "turns": [
                {
                    "turn_id": "turn-1",
                    "timeline": [
                        {
                            "type": "tool_call",
                            "tool_call_id": "large",
                            "name": "future_tool",
                            "arguments": {},
                        },
                        {
                            "type": "tool_result",
                            "tool_call_id": "large",
                            "name": "future_tool",
                            "ok": True,
                            "result": {
                                "payload": {
                                    "unknown_shape": "大结果" * 2000,
                                }
                            },
                        },
                        {
                            "type": "tool_call",
                            "tool_call_id": "small",
                            "name": "future_tool",
                            "arguments": {},
                        },
                        {
                            "type": "tool_result",
                            "tool_call_id": "small",
                            "name": "future_tool",
                            "ok": True,
                            "result": {"value": 1},
                        },
                    ],
                }
            ],
        }

        projected = project_recent_turns_for_planner(
            document,
            compact_tool_results=True,
        )
        results = [
            item["result"]
            for item in projected["turns"][0]["timeline"]
            if item["type"] == "tool_result"
        ]

        self.assertTrue(results[0]["truncated"])
        self.assertGreater(results[0]["original_tokens"], 768)
        self.assertEqual(results[0]["shown_tokens"], 512)
        self.assertIn("head", results[0])
        self.assertIn("tail", results[0])
        self.assertEqual(results[1], {"value": 1})

    def test_recent_episode_window_is_injected_without_keyword_recall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            episodes = [
                (
                    f"recent-topic-{index:02d}",
                    f"最近六小时话题 {index:02d}",
                    f"recent-turn-{index:02d}",
                    now - index * 60,
                )
                for index in range(13)
            ]
            episodes.append(
                ("old-topic", "六小时前旧话题", "old-turn", now - 7 * 3600)
            )
            for episode_id, title, turn_id, timestamp in episodes:
                store.create_episode(title, episode_id=episode_id)
                store.begin_turn(turn_id, "autonomous", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, '[]', 'internal')""",
                        (turn_id, f"{title}的内容", timestamp),
                    )
                    store._db.execute(
                        """UPDATE turns SET state='completed', updated_at=?
                           WHERE id=?""",
                        (timestamp, turn_id),
                    )
                store.link_turn_to_episode(episode_id, turn_id)

            empty_plan = plan("")
            retrieval = build_plan_retrieval(
                store,
                empty_plan,
                config(directory, recent_episode_hours=6),
            )

            self.assertEqual(
                [item["episode_id"] for item in retrieval["episodes"]],
                [f"recent-topic-{index:02d}" for index in range(6)],
            )
            self.assertTrue(
                all(item["relation"] == "recent" for item in retrieval["episodes"])
            )
            self.assertTrue(
                all(item["unit_ids"] == [] for item in retrieval["episodes"])
            )
            assembled = assemble_main_context(store, retrieval, 2000, 2000)
            self.assertIn("最近六小时话题 05", assembled["episodes"])
            self.assertNotIn("最近六小时话题 06", assembled["episodes"])
            self.assertNotIn("六小时前旧话题", assembled["episodes"])

            disabled = build_plan_retrieval(
                store,
                empty_plan,
                config(directory, recent_episode_hours=0),
            )
            self.assertEqual(disabled["episodes"], [])
            store.close()

    def test_recent_episode_is_injected_without_keyword_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            store.create_episode("项目邮件", episode_id="episode-mail")
            store.begin_turn("mail-turn", "autonomous", ["mail-turn"])
            with store._db:
                store._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at,
                        source_event_ids_json, delivery_state)
                       VALUES ('mail-turn', 'assistant', '项目邮件仍在等待',
                               ?, '[]', 'internal')""",
                    (now - 60,),
                )
                store._db.execute(
                    """UPDATE turns SET state='completed', updated_at=?
                       WHERE id='mail-turn'""",
                    (now - 60,),
                )
            store.link_turn_to_episode("episode-mail", "mail-turn")

            retrieval = build_plan_retrieval(
                store,
                plan("项目邮件"),
                config(directory, recent_episode_hours=6),
            )

            self.assertEqual(len(retrieval["episodes"]), 1)
            self.assertEqual(
                retrieval["episodes"][0]["episode_id"], "episode-mail"
            )
            self.assertEqual(
                retrieval["episodes"][0]["relation"], "recent"
            )
            self.assertEqual(retrieval["episodes"][0]["unit_ids"], ["mail"])
            store.close()

    def test_recent_and_recalled_episode_groups_are_capped_and_deduplicated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()

            def add_episode(episode_id: str, title: str, timestamp: float) -> None:
                turn_id = f"turn-{episode_id}"
                store.create_episode(title, episode_id=episode_id)
                store.begin_turn(turn_id, "autonomous", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, '[]', 'internal')""",
                        (turn_id, title, timestamp),
                    )
                    store._db.execute(
                        """UPDATE turns SET state='completed', updated_at=?
                           WHERE id=?""",
                        (timestamp, turn_id),
                    )
                store.link_turn_to_episode(episode_id, turn_id)

            for index in range(8):
                title = "旧甲 近期重叠" if index == 0 else f"近期话题 {index}"
                add_episode(f"recent-{index}", title, now - index * 60)
            for index in range(4):
                add_episode(
                    f"old-a-{index}",
                    f"旧甲 历史话题 {index}",
                    now - 7 * 3600 - index * 60,
                )
                add_episode(
                    f"old-b-{index}",
                    f"旧乙 历史话题 {index}",
                    now - 8 * 3600 - index * 60,
                )

            recall_plan = plan("旧甲")
            recall_plan["intent_units"][0]["recall_queries"] = ["旧甲", "旧乙"]
            retrieval = build_plan_retrieval(
                store,
                recall_plan,
                config(directory, recent_episode_hours=6, summary_results=12),
            )
            episode_ids = [
                str(item["episode_id"]) for item in retrieval["episodes"]
            ]
            recent_count = sum(
                item["relation"] == "recent" for item in retrieval["episodes"]
            )
            recalled_count = sum(
                item["relation"] == "recalled" for item in retrieval["episodes"]
            )

            self.assertEqual(recent_count, 6)
            self.assertLessEqual(recalled_count, 6)
            self.assertLessEqual(len(episode_ids), 12)
            self.assertEqual(len(episode_ids), len(set(episode_ids)))
            self.assertEqual(episode_ids.count("recent-0"), 1)
            store.close()

    def test_old_keyword_episodes_are_not_automatically_recalled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            for index in range(13):
                episode_id = f"keyword-{index:02d}"
                turn_id = f"keyword-turn-{index:02d}"
                marker = "关键词" if index < 4 else "其他"
                store.create_episode(
                    f"{marker}话题 {index:02d}", episode_id=episode_id
                )
                store.begin_turn(turn_id, "autonomous", [turn_id])
                timestamp = now - 7 * 3600 - index * 60
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, '[]', 'internal')""",
                        (turn_id, f"{marker}内容 {index:02d}", timestamp),
                    )
                    store._db.execute(
                        """UPDATE turns SET state='completed', updated_at=?
                           WHERE id=?""",
                        (timestamp, turn_id),
                    )
                store.link_turn_to_episode(episode_id, turn_id)

            retrieval = build_plan_retrieval(
                store,
                plan("关键词"),
                config(
                    directory,
                    recent_episode_hours=6,
                    summary_results=12,
                ),
            )

            self.assertTrue(retrieval["episodes"])
            self.assertEqual(retrieval["episodes"][0]["relation"], "recalled")
            self.assertTrue(
                all(
                    str(item["episode_id"]) in {f"keyword-{index:02d}" for index in range(4)}
                    for item in retrieval["episodes"]
                )
            )
            store.close()

    def test_episode_baseline_contains_recent_episodes_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            episodes = (
                ("recent-multi", "甲关键词 乙关键词", now - 300),
                ("old-multi", "甲关键词 乙关键词", now - 7 * 3600),
                ("recent-single", "甲关键词", now - 200),
                ("old-single", "甲关键词", now - 7 * 3600 - 60),
                ("recent-only", "无关近期话题", now - 100),
            )
            for episode_id, title, timestamp in episodes:
                turn_id = f"turn-{episode_id}"
                store.create_episode(title, episode_id=episode_id)
                store.begin_turn(turn_id, "autonomous", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, '[]', 'internal')""",
                        (turn_id, title, timestamp),
                    )
                    store._db.execute(
                        """UPDATE turns SET state='completed', updated_at=?
                           WHERE id=?""",
                        (timestamp, turn_id),
                    )
                store.link_turn_to_episode(episode_id, turn_id)

            retrieval = build_plan_retrieval(
                store,
                plan("甲关键词 | 乙关键词"),
                config(
                    directory,
                    recent_episode_hours=6,
                    summary_results=12,
                ),
            )

            episode_ids = [item["episode_id"] for item in retrieval["episodes"]]
            self.assertEqual(
                episode_ids,
                [
                    "recent-multi",
                    "recent-single",
                    "old-multi",
                    "recent-only",
                ],
            )
            store.close()

    def test_recent_episode_window_is_independent_of_directory_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("旧暗号", episode_id="old-secret")
            event = IncomingMessage(
                "old-secret-event",
                "old-secret-event",
                "朱红钥匙藏在温室花盆下面",
                1,
                1,
            )
            store.add_event(event)
            store.begin_turn("old-secret-turn", "owner", [event.event_id])
            store.commit_turn(
                [event], event.text, AgentReply(["记住了"]), turn_id="old-secret-turn"
            )
            store.link_turn_to_episode("old-secret", "old-secret-turn")
            for index in range(65):
                store.create_episode(f"较新的主题 {index}", episode_id=f"newer-{index}")
            store.create_episode("当前话题", episode_id="current-topic")

            self.assertNotIn(
                "old-secret",
                {item["id"] for item in store.list_episode_directory(64)},
            )
            expanded = plan("朱红钥匙 | 温室 | 花盆", "current-topic")
            retrieval = build_plan_retrieval(store, expanded, config(directory))

            self.assertIn(
                "old-secret",
                {item["episode_id"] for item in retrieval["episodes"]},
            )
            assembled = assemble_main_context(store, retrieval, 2000, 2000)
            self.assertIn("title: 旧暗号", assembled["episodes"])
            self.assertNotIn("朱红钥匙藏在温室花盆下面", assembled["episodes"])
            store.close()

    def test_automatic_recall_returns_directory_without_raw_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("很久以前的暗号", episode_id="long-match")
            secret = "紫罗兰火车票在银色抽屉"
            event = IncomingMessage(
                "long-match-event",
                "long-match-event",
                "甲" * 700 + secret,
                1,
                1,
            )
            store.add_event(event)
            store.begin_turn("long-match-turn", "owner", [event.event_id])
            stored_plan = plan(secret, "long-match")
            stored_plan["intent_units"][0]["event_ids"] = [event.event_id]
            store.save_context_plan(
                "long-match-turn", 1, [event.event_id], stored_plan
            )
            store.commit_turn(
                [event], event.text, AgentReply(["记下了"]), turn_id="long-match-turn"
            )
            store._db.execute(
                """UPDATE conversation_episodes
                   SET working_summary='曾经谈过一个暗号',
                       summarized_through_ordinal=1
                   WHERE id='long-match'"""
            )
            store._db.commit()

            recalled = recall_episode_context(store, secret, 3, 1000, 1000)

            self.assertIn("summary_quality: empty", recalled)
            self.assertNotIn("曾经谈过一个暗号", recalled)
            self.assertNotIn("matched_raw", recalled)
            self.assertNotIn(secret, recalled)
            store.close()

    def test_multi_intent_turn_indexes_only_its_bound_episode_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("邮件事项", episode_id="mail")
            store.create_episode("微博事项", episode_id="social")
            event = IncomingMessage(
                "mixed", "mixed", "检查 SMTP 邮件，然后刷微博看猫", 1, 1
            )
            store.add_event(event)
            store.begin_turn("mixed", "owner", [event.event_id])
            mixed_plan = {
                "version": 1,
                "intent_units": [
                    {
                        "id": "mail-unit",
                        "event_ids": [event.event_id],
                        "text": "检查 SMTP 邮件",
                        "intent": "check mail",
                        "references": [],
                        "recall_queries": ["SMTP 邮件"],
                    },
                    {
                        "id": "social-unit",
                        "event_ids": [event.event_id],
                        "text": "刷微博看猫",
                        "intent": "browse social media",
                        "references": [],
                        "recall_queries": ["微博 看猫"],
                    },
                ],
                "episode_actions": [
                    {
                        "action": "continue",
                        "episode_id": "mail",
                        "is_new": False,
                        "title": "邮件事项",
                        "relation": "primary",
                        "unit_ids": ["mail-unit"],
                        "topics": ["SMTP", "邮件"],
                        "entities": [],
                        "open_loops": [],
                        "salience": 0.5,
                    },
                    {
                        "action": "continue",
                        "episode_id": "social",
                        "is_new": False,
                        "title": "微博事项",
                        "relation": "related",
                        "unit_ids": ["social-unit"],
                        "topics": ["微博", "猫"],
                        "entities": [],
                        "open_loops": [],
                        "salience": 0.5,
                    },
                ],
                "episode_links": [],
                "uncertainty": [],
            }
            store.save_context_plan("mixed", 1, [event.event_id], mixed_plan)
            store.commit_turn(
                [event], event.text, AgentReply(["处理完成"]), turn_id="mixed"
            )

            self.assertEqual(
                [item["id"] for item in store.search_episodes("SMTP", 5)],
                ["mail"],
            )
            self.assertEqual(
                [item["id"] for item in store.search_episodes("看猫", 5)],
                ["social"],
            )
            store._db.execute("DELETE FROM episode_message_recall_terms")
            store._db.execute("DELETE FROM episode_recall_terms")
            store._db.commit()
            store.close()

            reopened = Store(Path(directory) / "momoi.sqlite3")
            self.assertEqual(
                [item["id"] for item in reopened.search_episodes("SMTP", 5)],
                ["mail"],
            )
            reopened.close()

    def test_recent_turn_and_core_identity_survive_unrelated_degraded_recall(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            previous = IncomingMessage(
                "previous", "previous", "把部署方式切换成蓝绿发布", 1, 1
            )
            store.add_event(previous)
            store.commit_turn(
                [previous],
                previous.text,
                AgentReply(["好，我接下来就按蓝绿发布处理"]),
                turn_id="previous",
            )
            with store._db:
                store._db.execute(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('profile', 'owner.name', '主人的名字是 Sakana',
                               'always', 'owner', 'owner-name', '我叫 Sakana', 1, 1, 1)"""
                )
            degraded = {
                "version": 1,
                "intent_units": [
                    {
                        "id": "u1",
                        "event_ids": ["current"],
                        "text": "好，就这么做",
                        "intent": "degraded_message_segment",
                        "references": [],
                        "recall_queries": ["好，就这么做"],
                    }
                ],
                "episode_actions": [],
                "episode_links": [],
                "uncertainty": ["planner failed"],
            }

            retrieval = build_plan_retrieval(store, degraded, config(directory))
            assembled = assemble_main_context(
                store, retrieval, 2000, 2000, recent_turns=1
            )

            self.assertIn("蓝绿发布", assembled["recent_turns"])
            self.assertIn("Sakana", assembled["long_term_memories"])
            store.close()

    def test_recall_parts_include_bounded_matching_turn_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("一次旧聊天", episode_id="episode-old")
            event = IncomingMessage(
                "rare-event",
                "rare-event",
                "蓝色保温杯藏在阁楼第三个纸箱里",
                1,
                1,
            )
            store.add_event(event)
            store.begin_turn("rare-turn", "owner", [event.event_id])
            stored_plan = plan("蓝色保温杯 第三个纸箱", "episode-old")
            stored_plan["intent_units"][0]["event_ids"] = [event.event_id]
            store.save_context_plan("rare-turn", 1, [event.event_id], stored_plan)
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["我记得这个位置了"]),
                turn_id="rare-turn",
            )
            store._db.execute(
                """UPDATE conversation_episodes
                   SET working_summary='聊过家中物品的位置',
                       summarized_through_ordinal=1
                   WHERE id='episode-old'"""
            )

            recalled, recalled_turns = recall_episode_context_parts(
                store, "蓝色保温杯 | 第三个纸箱", 3, 1000, 1000
            )
            self.assertIn("summary_quality: empty", recalled)
            self.assertNotIn("聊过家中物品的位置", recalled)
            self.assertNotIn("蓝色保温杯藏在阁楼第三个纸箱里", recalled)
            self.assertIn("蓝色保温杯藏在阁楼第三个纸箱里", recalled_turns)
            self.assertIn("[recalled turn=rare-turn]", recalled_turns)
            self.assertNotIn("matched:", recalled_turns)
            retrieval = build_plan_retrieval(
                store,
                plan("蓝色保温杯 | 第三个纸箱", "episode-old"),
                config(directory),
            )
            assembled = assemble_main_context(
                store,
                retrieval,
                1000,
                2000,
                recent_turns=1,
            )
            self.assertIn("蓝色保温杯藏在阁楼第三个纸箱里", assembled["recent_turns"])
            self.assertNotIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                assembled["recalled_turns"],
            )
            self.assertIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                store.conversation_episode("episode-old")["messages"][0]["content"],
            )
            store._db.execute("DELETE FROM episode_message_recall_terms")
            store._db.execute("DELETE FROM episode_recall_terms")
            store._db.commit()
            store.close()

            reopened = Store(Path(directory) / "momoi.sqlite3")
            _, reopened_turns = recall_episode_context_parts(
                reopened, "蓝色保温杯 | 第三个纸箱", 3, 1000, 1000
            )
            self.assertIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                reopened_turns,
            )
            reopened.close()

    def test_commit_without_a_context_plan_stays_unassigned_for_consolidation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage(
                "fallback", "fallback", "规划器失败时也别忘记我", 1, 1
            )
            store.add_event(event)
            store.commit_turn(
                [event], event.text, AgentReply(["这轮仍然会归档"]), turn_id="fallback"
            )
            outbox_id = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='fallback'"
            ).fetchone()["id"]
            store.mark_sent(int(outbox_id))

            self.assertEqual(store.search_episodes("规划器失败 归档", 3), [])
            candidate = store.claim_episode_consolidation_candidate()
            self.assertEqual(candidate["turns"][0]["turn_id"], "fallback")
            self.assertNotIn(
                "这轮仍然会归档",
                recall_episode_context(store, "规划器失败 归档", 3, 1000, 1000),
            )
            store.mark_ambiguous(int(outbox_id), 1, "timeout")
            self.assertNotIn(
                "这轮仍然会归档",
                recall_episode_context(store, "规划器失败 归档", 3, 1000, 1000),
            )
            store.mark_sent(int(outbox_id))
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM episode_turns WHERE turn_id='fallback'"
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_recall_and_context_include_only_plan_relevant_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("项目邮件", episode_id="episode-mail")
            store.create_episode("微博闲聊", episode_id="episode-social")
            for turn_id, event_id, text, reply, episode_id in (
                (
                    "turn-mail",
                    "mail-event",
                    "项目邮件还没到",
                    "我会记着这封项目邮件",
                    "episode-mail",
                ),
                (
                    "turn-social",
                    "social-event",
                    "微博上有只猫",
                    "那只猫很可爱",
                    "episode-social",
                ),
            ):
                event = IncomingMessage(event_id, event_id, text, 1, 1)
                store.add_event(event)
                store.begin_turn(turn_id, "owner", [event_id])
                store.commit_turn([event], text, AgentReply([reply]), turn_id=turn_id)
                store.link_turn_to_episode(episode_id, turn_id)

            now = time.time()
            store._db.executemany(
                """INSERT INTO memories
                   (kind, key, content, authority, source_event_id, evidence_quote,
                    importance, created_at, updated_at)
                   VALUES ('episodic', ?, ?, 'owner', ?, ?, 0.8, ?, ?)""",
                [
                    (
                        "project.mail.waiting",
                        "主人正在等待项目邮件",
                        "mail-event",
                        "项目邮件还没到",
                        now,
                        now,
                    ),
                    (
                        "social.cat",
                        "主人看到了微博上的猫",
                        "social-event",
                        "微博上有只猫",
                        now,
                        now,
                    ),
                ],
            )
            store._db.execute(
                """INSERT INTO reflections
                   (id, local_date, state, scheduled_at, created_at, completed_at)
                   VALUES ('reflection:test', '2030-01-01', 'completed', ?, ?, ?)""",
                (now, now, now),
            )
            store._db.executemany(
                """INSERT INTO reflection_memories
                   (kind, key, content, evidence, confidence,
                    source_reflection_id, created_at, updated_at)
                   VALUES (?, ?, ?, 'evidence', 0.8, 'reflection:test', ?, ?)""",
                [
                    (
                        "shared_experience",
                        "project.mail.context",
                        "项目邮件关系到当前合作",
                        now,
                        now,
                    ),
                    (
                        "shared_experience",
                        "social.cat.context",
                        "微博猫是一次闲聊",
                        now,
                        now,
                    ),
                ],
            )
            store.create_episode(
                "较早的项目邮件",
                episode_id="episode-mail-history",
                topics=["项目邮件"],
            )
            store.create_episode(
                "较早的微博闲聊",
                episode_id="episode-social-history",
                topics=["微博"],
            )
            store._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', summary='较早的项目邮件仍在等待', closed_at=?
                   WHERE id='episode-mail-history'""",
                (now,),
            )
            store._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', summary='最近聊过微博上的猫', closed_at=?
                   WHERE id='episode-social-history'""",
                (now,),
            )
            store._db.executemany(
                """INSERT INTO goals
                   (id, title, success_criteria, authority, source_event_id,
                    status, plan_json, next_action, created_at, updated_at)
                   VALUES (?, ?, '完成', 'owner', 'source', 'active', '[]', ?, ?, ?)""",
                [
                    ("goal-mail", "跟进项目邮件", "检查项目邮件", now, now),
                    ("goal-social", "整理微博收藏", "整理微博", now, now),
                ],
            )
            store._db.executemany(
                """INSERT INTO reminders
                   (id, text, source_event_id, status, fire_at, created_at, updated_at)
                   VALUES (?, ?, 'source', 'pending', ?, ?, ?)""",
                [
                    ("reminder-mail", "检查项目邮件", now + 60, now, now),
                    ("reminder-social", "看看微博收藏", now + 60, now, now),
                ],
            )
            store._db.commit()

            retrieval = build_plan_retrieval(
                store, plan("项目邮件"), config(directory)
            )
            assembled = assemble_main_context(store, retrieval, 2000, 2000)

            self.assertTrue(retrieval["recall_memories"])
            self.assertEqual(
                [item["id"] for item in retrieval["goals"]],
                ["goal-mail", "goal-social"],
            )
            self.assertEqual(
                [item["id"] for item in retrieval["reminders"]],
                ["reminder-mail", "reminder-social"],
            )
            rendered = "\n".join(assembled.values())
            self.assertNotIn("较早的项目邮件仍在等待", rendered)
            self.assertIn("项目邮件关系到当前合作", rendered)
            self.assertIn("goal-mail", rendered)
            self.assertIn("goal-social", rendered)
            self.assertIn("reminder-social", rendered)
            autonomous = recall_episode_context(store, "项目邮件", 3, 2000, 2000)
            self.assertNotIn("较早的项目邮件仍在等待", autonomous)
            self.assertIn("summary_quality: empty", autonomous)
            self.assertNotIn("最近聊过微博上的猫", autonomous)
            bounded_tail = store.episode_messages("episode-mail", 5)
            self.assertLessEqual(
                sum(estimate_tokens(str(item["content"])) for item in bounded_tail),
                5,
            )
            store.close()

    def test_context_plan_no_longer_executes_memory_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            store._db.executemany(
                """INSERT INTO memories
                   (kind, key, content, authority, source_event_id, evidence_quote,
                    importance, created_at, updated_at)
                   VALUES ('episodic', ?, ?, 'owner', 'source', 'evidence', ?, ?, ?)""",
                [
                    ("mail.one", "邮件事项一", 0.9, now, now),
                    ("mail.two", "邮件事项二", 0.8, now, now),
                    ("social.one", "微博事项", 0.7, now, now),
                    ("weather.one", "天气事项", 0.7, now, now),
                    ("music.one", "音乐事项", 0.7, now, now),
                ],
            )
            store._db.commit()
            split_plan = plan("邮件")
            split_plan["intent_units"].append(
                {
                    "id": "social",
                    "event_ids": ["current"],
                    "text": "微博",
                    "intent": "social",
                    "references": [],
                }
            )
            split_plan["intent_units"].extend(
                [
                    {
                        "id": "weather",
                        "event_ids": ["current"],
                        "text": "天气",
                        "intent": "weather",
                        "references": [],
                    },
                    {
                        "id": "music",
                        "event_ids": ["current"],
                        "text": "音乐",
                        "intent": "music",
                        "references": [],
                    },
                ]
            )
            split_plan["episode_actions"][0]["unit_ids"].extend(
                ["social", "weather", "music"]
            )

            retrieval = build_plan_retrieval(
                store, split_plan, config(directory, memory_results=2)
            )
            self.assertTrue(retrieval["recall_memories"])
            store.close()
