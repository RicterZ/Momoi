import json
import tempfile
import time
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.context_time import context_timestamp
from momoi.models import AgentReply, IncomingMessage
from momoi.runtime.context_assembler import (
    _episode_header,
    assemble_main_context,
    assemble_recent_external_events,
    build_plan_retrieval,
    recall_episode_context,
    select_plan_recall_queries,
)
from momoi.runtime.transcript import build_transcript
from momoi.storage import Store, estimate_tokens
from momoi.storage.episode_ranking import rank_recall_items


def config(
    directory: str,
    memory_results: int = 6,
    summary_results: int = 3,
) -> AppConfig:
    return AppConfig(
        llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
        channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        system_prompt="test",
        transcript_turns_min=4,
        transcript_turns_max=4,
        episode_raw_tail_turns=2,
        memory_results=memory_results,
        summary_results=summary_results,
        summary_tokens=2000,
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
                "recall_queries": [
                    {
                        "semantic": query,
                        "keywords": query.split(" | "),
                    }
                ],
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
    def test_structured_recall_need_separates_sparse_and_dense_queries(self) -> None:
        recall_plan = plan("legacy")
        recall_plan["intent_units"] = [
            {
                "id": "first",
                "recall_queries": [
                    {
                        "semantic": "老师此前如何处理客厅设备",
                        "keywords": ["客厅设备", "device-42"],
                    }
                ],
            },
            {
                "id": "second",
                "recall_queries": [
                    {
                        "semantic": "老师此前如何处理客厅设备",
                        "keywords": ["设备别名", "device-42"],
                    },
                    {
                        "semantic": "老师对自动操作的长期偏好",
                        "keywords": [],
                    },
                ],
            },
        ]

        selected, reused, emitted, skipped = select_plan_recall_queries(recall_plan)

        self.assertEqual(reused, {})
        self.assertEqual(skipped, set())
        self.assertEqual(
            emitted,
            {"老师此前如何处理客厅设备", "老师对自动操作的长期偏好"},
        )
        self.assertEqual(
            selected,
            [
                {
                    "expression": "客厅设备|device-42|设备别名",
                    "semantic_expression": "老师此前如何处理客厅设备",
                    "keywords": ["客厅设备", "device-42", "设备别名"],
                    "unit_ids": ["first", "second"],
                    "priority": 0,
                },
                {
                    "expression": "",
                    "semantic_expression": "老师对自动操作的长期偏好",
                    "keywords": [],
                    "unit_ids": ["second"],
                    "priority": 1,
                },
            ],
        )

    def test_legacy_context_record_is_normalized_at_storage_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("legacy", "owner", ["legacy-event"])
            legacy_plan = plan("unused")
            legacy_plan["intent_units"][0]["recall_queries"] = [
                "设备名称 | device-42"
            ]
            with store._db:
                store._db.execute(
                    """INSERT INTO context_plans
                       (turn_id, revision, source_event_ids_json, plan_json,
                        retrieval_json, state, created_at, updated_at)
                       VALUES ('legacy', 1, '["legacy-event"]', ?, ?,
                               'recalled', 1, 1)""",
                    (
                        json.dumps(legacy_plan, ensure_ascii=False),
                        json.dumps(
                            {
                                "version": 5,
                                "query_recall": (
                                    "queries=设备名称 | device-42\n"
                                    "hits=设备名称 | device-42"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

            record = store.context_plan("legacy")
            self.assertEqual(record["retrieval"]["version"], 6)
            self.assertEqual(
                record["retrieval"]["effective_recall_queries"],
                ["设备名称 | device-42"],
            )
            selected, _reused, _emitted, _skipped = select_plan_recall_queries(
                record["plan"]
            )
            self.assertEqual(selected[0]["expression"], "设备名称|device-42")
            self.assertEqual(selected[0]["keywords"], ["设备名称", "device-42"])
            store.close()

    def test_plan_recall_reuse_does_not_repeat_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            reuse_plan = plan("unused")
            unit = reuse_plan["intent_units"][0]
            unit["recall"] = {
                "mode": "reuse",
                "from_turn_id": "prior-turn",
            }
            unit["recall_queries"] = []

            retrieval = build_plan_retrieval(
                store,
                reuse_plan,
                config(directory),
            )

            self.assertEqual(retrieval["recall_memories"], [])
            self.assertEqual(retrieval["reflection_memories"], [])
            self.assertEqual(
                retrieval["query_recall"],
                "reused_from=prior-turn units=mail",
            )
            self.assertEqual(retrieval["effective_recall_queries"], [])
            store.close()

    def test_plan_recall_reuse_inherits_source_evidence_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("prior-turn", "owner", ["prior-event"])
            store.save_context_plan(
                "prior-turn",
                1,
                ["prior-event"],
                {
                    "intent_units": [
                        {
                            "id": "prior",
                            "recall": {
                                "mode": "search",
                                "queries": ["亲密互动|暧昧打闹|晚间玩闹"],
                            },
                            "recall_queries": [
                                {
                                    "semantic": "亲密互动|暧昧打闹|晚间玩闹",
                                    "keywords": ["亲密互动", "暧昧打闹", "晚间玩闹"],
                                }
                            ],
                            "recall_from_turn_id": "",
                        }
                    ]
                },
            )
            store.save_context_retrieval(
                "prior-turn",
                1,
                {
                    "version": 4,
                    "episodes": [
                        {
                            "episode_id": "play-history",
                            "relation": "recalled",
                            "is_new": False,
                            "matches": [],
                            "unit_ids": ["prior"],
                            "last_activity_at": time.time(),
                            "salience": 0.7,
                            "matched_keywords": ["晚间玩闹"],
                            "keyword_match_count": 1,
                            "search_score": 1.0,
                            "semantic_score": 1.0,
                            "matched_queries": [
                                {
                                    "expression": "亲密互动|暧昧打闹|晚间玩闹",
                                    "unit_ids": ["prior"],
                                }
                            ],
                            "is_recent": False,
                        }
                    ],
                    "recall_memories": [
                        {
                            "kind": "shared",
                            "key": "relationship.play",
                            "content": "老师和小桃常会亲密玩闹",
                            "unit_ids": ["prior"],
                        }
                    ],
                    "reflection_memories": [],
                    "query_recall": (
                        "queries=亲密互动|暧昧打闹|晚间玩闹\n"
                        "hits=亲密互动|暧昧打闹|晚间玩闹"
                    ),
                    "effective_recall_queries": [
                        "亲密互动|暧昧打闹|晚间玩闹"
                    ],
                },
            )
            reuse_plan = plan("unused")
            unit = reuse_plan["intent_units"][0]
            unit["recall"] = {
                "mode": "reuse",
                "from_turn_id": "prior-turn",
            }
            unit["recall_queries"] = []

            retrieval = build_plan_retrieval(
                store,
                reuse_plan,
                config(directory),
            )

            self.assertEqual(
                retrieval["effective_recall_queries"],
                ["亲密互动|暧昧打闹|晚间玩闹"],
            )
            self.assertEqual(
                retrieval["recall_memories"],
                [
                    {
                        "kind": "shared",
                        "key": "relationship.play",
                        "content": "老师和小桃常会亲密玩闹",
                        "unit_ids": ["mail"],
                    }
                ],
            )
            inherited_episode = next(
                item
                for item in retrieval["episodes"]
                if item.get("episode_id") == "play-history"
            )
            self.assertEqual(inherited_episode["unit_ids"], ["mail"])
            store.close()

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
                    "recall_queries": [
                        {
                            "semantic": f"{unit_id}{index}",
                            "keywords": [f"{unit_id}{index}"],
                        }
                        for index in range(1, 4)
                    ],
                }
                for unit_id in ("a", "b", "c")
            ]
            recall_plan["episode_actions"][0]["unit_ids"] = ["a", "b", "c"]

            retrieval = build_plan_retrieval(
                store,
                recall_plan,
                config(directory),
            )

            self.assertIn(
                "queries=a1 | b1 | c1 | a2 | b2 | c2",
                retrieval["query_recall"],
            )
            self.assertIn("query_count=6/9", retrieval["query_recall"])
            store.close()

    def test_recall_ranking_uses_continuous_relevance_before_recency(self) -> None:
        ranked = rank_recall_items(
            [
                {
                    "turn_id": "older-strong",
                    "search_score": 2.4,
                    "last_activity_at": 10,
                },
                {
                    "turn_id": "recent-weak",
                    "search_score": 0.7,
                    "last_activity_at": 99,
                },
                {
                    "turn_id": "recent-only",
                    "last_activity_at": 100,
                },
            ],
            now=100,
        )

        self.assertEqual(
            [item["turn_id"] for item in ranked],
            ["older-strong", "recent-weak", "recent-only"],
        )






    def test_retrieval_keeps_fixed_and_dynamic_memory_layers_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("蓝色杯子共同回忆", episode_id="cup")
            now = time.time()
            always_content = "长期记得蓝色杯子" + "很重要" * 500 + "长期正文结尾"
            recall_content = "召回蓝色杯子的旧位置" + "在书房" * 100 + "召回正文结尾"
            with store._db:
                store._db.executemany(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('profile', ?, ?, ?, 'owner', 'source', 'evidence',
                               0.8, ?, ?)""",
                    [
                        ("fixed.cup", always_content, "always", now, now),
                        ("recalled.cup", recall_content, "recall", now, now),
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
            self.assertIn("长期正文结尾", retrieval["long_term_memories"])
            recalled = "\n".join(
                str(item["content"]) for item in retrieval["recall_memories"]
            )
            self.assertIn("召回蓝色杯子的旧位置", recalled)
            self.assertIn("召回正文结尾", recalled)
            self.assertNotIn("长期记得蓝色杯子", recalled)
            reflections = "\n".join(
                str(item["content"]) for item in retrieval["reflection_memories"]
            )
            self.assertIn("复盘认为蓝色杯子很重要", reflections)
            self.assertIn("那天一起找过蓝色杯子", reflections)
            self.assertNotIn("复盘认为主人喜欢猫", reflections)
            self.assertTrue(
                all(
                    item["local_date"] == "2030-01-01"
                    for item in retrieval["reflection_memories"]
                )
            )
            assembled = assemble_main_context(store, retrieval, 2000)
            self.assertIn(
                "may be outdated or no longer applicable",
                assembled["reflection_memories"],
            )
            self.assertIn(
                "[date=2030-01-01 owner_profile:cup.core]",
                assembled["reflection_memories"],
            )

            heartbeat_retrieval = build_plan_retrieval(
                store,
                {
                    "activity": {
                        "intent": "整理蓝色杯子的共同回忆",
                        "reason": "想回顾这件事",
                        "recall_queries": [
                            {"semantic": "蓝色杯子", "keywords": ["蓝色杯子"]}
                        ],
                    }
                },
                config(directory),
            )
            self.assertTrue(heartbeat_retrieval["recall_memories"])
            self.assertTrue(heartbeat_retrieval["reflection_memories"])
            self.assertIn("queries=蓝色杯子", heartbeat_retrieval["query_recall"])

            shared_intent = "整理蓝色杯子的共同回忆"
            owner_episode_retrieval = build_plan_retrieval(
                store,
                {
                    "intent_units": [
                        {
                            "id": "u1",
                            "intent": shared_intent,
                            "recall_queries": [
                                {"semantic": "蓝色杯子", "keywords": ["蓝色杯子"]}
                            ],
                        }
                    ]
                },
                config(directory),
            )
            heartbeat_episode_retrieval = build_plan_retrieval(
                store,
                {
                    "activity": {
                        "intent": shared_intent,
                        "recall_queries": [
                            {"semantic": "蓝色杯子", "keywords": ["蓝色杯子"]}
                        ],
                    }
                },
                config(directory),
            )
            self.assertEqual(
                [item["episode_id"] for item in owner_episode_retrieval["episodes"]],
                [
                    item["episode_id"]
                    for item in heartbeat_episode_retrieval["episodes"]
                ],
            )
            for owner_item, heartbeat_item in zip(
                owner_episode_retrieval["episodes"],
                heartbeat_episode_retrieval["episodes"],
                strict=True,
            ):
                self.assertAlmostEqual(
                    owner_item["search_score"],
                    heartbeat_item["search_score"],
                )
            store.close()

    def test_memory_results_limits_each_recall_kind_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.executemany(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('routine', ?, ?, 'recall', 'owner', 'source',
                               '空调', 0.9, ?, ?)""",
                    [
                        ("ac.confirmed.one", "空调确认经验一", now, now),
                        ("ac.confirmed.two", "空调确认经验二", now, now),
                    ],
                )
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:limit', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.executemany(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', ?, ?, '空调', 0.8,
                               'reflection:limit', ?, ?)""",
                    [
                        ("ac.reflection.one", "空调复盘经验一", now, now),
                        ("ac.reflection.two", "空调复盘经验二", now, now),
                    ],
                )

            retrieval = build_plan_retrieval(
                store,
                plan("空调"),
                config(directory, memory_results=2),
            )

            self.assertEqual(
                len(retrieval["recall_memories"])
                + len(retrieval["reflection_memories"]),
                4,
            )
            self.assertEqual(len(retrieval["recall_memories"]), 2)
            self.assertEqual(len(retrieval["reflection_memories"]), 2)
            store.close()

    def test_memory_recall_caps_each_kind_at_six(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.executemany(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('routine', ?, ?, 'recall', 'owner', 'source',
                               '空调', 0.9, ?, ?)""",
                    [
                        (f"ac.confirmed.{index}", f"空调确认经验{index}", now, now)
                        for index in range(7)
                    ],
                )
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:cap', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.executemany(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', ?, ?, '空调', 0.8,
                               'reflection:cap', ?, ?)""",
                    [
                        (f"ac.reflection.{index}", f"空调复盘经验{index}", now, now)
                        for index in range(7)
                    ],
                )

            retrieval = build_plan_retrieval(
                store,
                plan("空调"),
                config(directory, memory_results=20),
            )

            self.assertEqual(len(retrieval["recall_memories"]), 6)
            self.assertEqual(len(retrieval["reflection_memories"]), 6)
            store.close()

    def test_zero_memory_results_disables_both_recall_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.execute(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('routine', 'ac.confirmed', '空调经验', 'recall',
                               'owner', 'source', '空调', 1.0, ?, ?)""",
                    (now, now),
                )

            retrieval = build_plan_retrieval(
                store,
                plan("空调"),
                config(directory, memory_results=0),
            )

            self.assertEqual(retrieval["recall_memories"], [])
            self.assertEqual(retrieval["reflection_memories"], [])
            store.close()



    def test_silent_external_events_fold_without_displacing_shared_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")

            for index in range(1, 7):
                event = IncomingMessage(
                    f"owner-event-{index}",
                    f"owner-event-{index}",
                    f"owner-{index}",
                    index,
                    index,
                )
                store.add_event(event)
                store.begin_turn(f"owner-turn-{index}", "owner", [event.event_id])
                store.commit_turn(
                    [event],
                    event.text,
                    AgentReply([f"assistant-{index}"]),
                    turn_id=f"owner-turn-{index}",
                )
                outbox_id = store._db.execute(
                    "SELECT id FROM outbox WHERE turn_id=?",
                    (f"owner-turn-{index}",),
                ).fetchone()["id"]
                store.mark_sent(int(outbox_id))

            baseline = store.recent_conversation_messages(6, 88000, 100)
            for index in range(12):
                turn_id = f"webhook:silent-{index}:0"
                observed_at = 20 + index
                store.begin_turn(turn_id, "webhook", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'event', '门锁超时未关', ?, '[]', 'delivered')""",
                        (turn_id, observed_at),
                    )
                    store._db.execute(
                        """UPDATE turns SET state='completed', stage='completed',
                           updated_at=? WHERE id=?""",
                        (observed_at, turn_id),
                    )

            selected = store.recent_conversation_messages(6, 88000, 100)
            self.assertEqual(selected, baseline)

            external = assemble_recent_external_events(
                store,
                100,
                lookback_seconds=100,
            )
            self.assertEqual(external.count("event: 门锁超时未关"), 1)
            self.assertIn("observations: 12 since", external)
            self.assertIn(context_timestamp(31, store.timezone), external)
            store.close()

    def test_visible_autonomous_event_remains_shared_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            turn_id = "webhook:visible:0"
            store.begin_turn(turn_id, "webhook", [turn_id])
            with store._db:
                store._db.executemany(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at,
                        source_event_ids_json, delivery_state)
                       VALUES (?, ?, ?, ?, '[]', 'delivered')""",
                    [
                        (turn_id, "event", "门锁超时未关", 10),
                        (turn_id, "assistant", "老师看一下门锁", 11),
                    ],
                )
                store._db.execute(
                    """UPDATE turns SET state='completed', stage='completed',
                       updated_at=11 WHERE id=?""",
                    (turn_id,),
                )

            recent = store.recent_conversation_messages(6, 88000, 20)
            transcript = build_transcript(recent, timezone=store.timezone)
            self.assertEqual(transcript.messages, [])
            self.assertEqual(
                [part for group in transcript.orphaned for part in group.parts],
                ["老师看一下门锁"],
            )
            self.assertEqual(
                assemble_recent_external_events(
                    store,
                    20,
                    lookback_seconds=20,
                ),
                "",
            )
            store.close()




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



    def test_recent_episodes_are_not_injected_without_recall(self) -> None:
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
                store.begin_turn(turn_id, "owner", [turn_id])
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
                config(directory),
            )

            self.assertEqual(retrieval["episodes"], [])
            assembled = assemble_main_context(store, retrieval, 2000)
            self.assertEqual(assembled["episodes"], "")
            store.close()

    def test_episode_is_injected_only_by_query_recall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            store.create_episode("项目邮件", episode_id="episode-mail")
            store.begin_turn("mail-turn", "owner", ["mail-turn"])
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
                config(directory),
            )

            self.assertEqual(len(retrieval["episodes"]), 1)
            self.assertEqual(
                retrieval["episodes"][0]["episode_id"], "episode-mail"
            )
            self.assertEqual(
                retrieval["episodes"][0]["relation"], "recalled"
            )
            self.assertEqual(retrieval["episodes"][0]["unit_ids"], ["mail"])
            store.close()

    def test_recalled_episode_groups_are_capped_and_deduplicated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()

            def add_episode(episode_id: str, title: str, timestamp: float) -> None:
                turn_id = f"turn-{episode_id}"
                store.create_episode(title, episode_id=episode_id)
                store.begin_turn(turn_id, "owner", [turn_id])
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
            recall_plan["intent_units"][0]["recall_queries"] = [
                {"semantic": "旧甲", "keywords": ["旧甲"]},
                {"semantic": "旧乙", "keywords": ["旧乙"]},
            ]
            retrieval = build_plan_retrieval(
                store,
                recall_plan,
                config(directory, summary_results=12),
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

            self.assertEqual(recent_count, 0)
            self.assertLessEqual(recalled_count, 12)
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
                store.begin_turn(turn_id, "owner", [turn_id])
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

    def test_episode_baseline_merges_recent_with_independent_alias_hits(
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
                store.begin_turn(turn_id, "owner", [turn_id])
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
                    summary_results=12,
                ),
            )

            episode_ids = [item["episode_id"] for item in retrieval["episodes"]]
            self.assertEqual(
                episode_ids,
                [
                    "recent-multi",
                    "old-multi",
                    "recent-single",
                    "old-single",
                ],
            )
            store.close()

    def test_query_recall_does_not_inject_unmatched_old_episode(self) -> None:
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

            self.assertNotIn(
                "old-secret",
                {item["episode_id"] for item in retrieval["episodes"]},
            )
            assembled = assemble_main_context(store, retrieval, 2000)
            self.assertNotIn("title: 旧暗号", assembled["episodes"])
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

            recalled = recall_episode_context(store, secret, 3, 1000)

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
                        "recall_queries": [
                            {"semantic": "SMTP 邮件", "keywords": ["SMTP", "邮件"]}
                        ],
                    },
                    {
                        "id": "social-unit",
                        "event_ids": [event.event_id],
                        "text": "刷微博看猫",
                        "intent": "browse social media",
                        "references": [],
                        "recall_queries": [
                            {"semantic": "微博 看猫", "keywords": ["微博", "看猫"]}
                        ],
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


    def test_turn_keywords_rank_episode_and_inject_matched_evidence(
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

            recalled = recall_episode_context(
                store, "蓝色保温杯 | 第三个纸箱", 3, 1000
            )
            self.assertIn("summary_quality: empty", recalled)
            self.assertNotIn("聊过家中物品的位置", recalled)
            self.assertIn("matched_evidence:", recalled)
            self.assertIn("蓝色保温杯藏在阁楼第三个纸箱里", recalled)
            retrieval = build_plan_retrieval(
                store,
                plan("蓝色保温杯 | 第三个纸箱", "episode-old"),
                config(directory),
            )
            self.assertEqual(retrieval["version"], 6)
            self.assertIn("episode_hits=", retrieval["query_recall"])
            self.assertNotIn("turn_hits=", retrieval["query_recall"])
            selected = next(
                item
                for item in retrieval["episodes"]
                if item["episode_id"] == "episode-old"
            )
            self.assertGreater(selected["search_score"], 0)
            self.assertIn(
                "rare-turn",
                {
                    turn_id
                    for query in selected["matched_queries"]
                    for turn_id in query["turn_ids"]
                },
            )
            assembled = assemble_main_context(
                store,
                retrieval,
                1000,
            )
            self.assertIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                "\n".join(assembled.values()),
            )
            self.assertIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                store.conversation_episode("episode-old")["messages"][0]["content"],
            )
            store.close()

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
            candidate = store.claim_episode_consolidation_candidate(minimum=1)
            self.assertEqual(candidate["turns"][0]["turn_id"], "fallback")
            self.assertNotIn(
                "这轮仍然会归档",
                recall_episode_context(store, "规划器失败 归档", 3, 1000),
            )
            store.mark_ambiguous(int(outbox_id), 1, "timeout")
            self.assertNotIn(
                "这轮仍然会归档",
                recall_episode_context(store, "规划器失败 归档", 3, 1000),
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
            store._db.commit()

            with self.assertLogs(
                "momoi.runtime.context_assembler", level="INFO"
            ) as captured:
                retrieval = build_plan_retrieval(
                    store, plan("项目邮件"), config(directory)
                )
            assembled = assemble_main_context(store, retrieval, 2000)

            recall_logs = {
                record.momoi_event: record.momoi_fields
                for record in captured.records
                if hasattr(record, "momoi_event")
            }
            self.assertEqual(
                recall_logs["context_recall"]["queries"][0]["expression"],
                "项目邮件",
            )
            self.assertTrue(
                any(
                    "主人正在等待项目邮件" in item["content"]
                    for item in recall_logs["context_recall_memory_results"][
                        "results"
                    ]
                )
            )
            self.assertTrue(
                any(
                    item["title"] == "项目邮件" and item["evidence"]
                    for item in recall_logs["context_recall_episode_results"][
                        "results"
                    ]
                )
            )
            self.assertNotIn("context_recall_turn_results", recall_logs)
            state_log = recall_logs["context_recall_state_results"]
            self.assertIn(
                "跟进项目邮件", [item["title"] for item in state_log["goals"]]
            )

            self.assertTrue(retrieval["recall_memories"])
            self.assertNotIn("recalled_turns", retrieval)
            self.assertEqual(
                [item["id"] for item in retrieval["goals"]],
                ["goal-mail", "goal-social"],
            )
            rendered = "\n".join(assembled.values())
            self.assertNotIn("较早的项目邮件仍在等待", rendered)
            self.assertIn("项目邮件关系到当前合作", rendered)
            self.assertIn("goal-mail", rendered)
            self.assertIn("goal-social", rendered)
            autonomous = recall_episode_context(store, "项目邮件", 3, 2000)
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
