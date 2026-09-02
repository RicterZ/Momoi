import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from momoi.agenda_tools import AGENDA_TOOL_POLICY, AGENDA_TOOL_SPECS, AgendaTools
from momoi.builtin_tools import BuiltinTools
from momoi.channel.napcat import NapCatConfig
from momoi.config import (
    AppConfig,
    HeartbeatConfig,
    LLMConfig,
    NotificationConfig,
)
from momoi.context_time import context_timestamp
from momoi.memory_tools import MEMORY_TOOL_POLICY, MEMORY_TOOL_SPECS, MemoryTools
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ToolCall,
    TurnDraft,
)
from momoi.runtime import (
    MomoiDaemon,
)
from momoi.storage import MemoryRecallQuery, Store, estimate_tokens
from momoi.storage.integrity import StorageIntegrityError
from momoi.storage.scheduling import next_schedule_at, normalize_schedule


class StorageMemoryTest(unittest.TestCase):
    def test_ranked_memory_recall_keeps_categories_separate_on_equal_match(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.execute(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('routine', 'home.ac.confirmed', '打开客厅空调',
                               'recall', 'owner', 'source', '打开客厅空调',
                               0.8, ?, ?)""",
                    (now, now),
                )
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:rank', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', 'home.ac.reflection', '打开客厅空调',
                               'evidence', 1.0, 'reflection:rank', ?, ?)""",
                    (now, now),
                )

            ranked = store.rank_recalled_memories(
                [MemoryRecallQuery("打开客厅空调", ("home",), 0)],
                1,
                now=now,
            )

            self.assertEqual(ranked[0]["source"], "confirmed")
            self.assertEqual(ranked[0]["key"], "home.ac.confirmed")
            self.assertEqual(ranked[1]["source"], "reflection")
            self.assertEqual(ranked[1]["key"], "home.ac.reflection")
            store.close()

    def test_ranked_memory_recall_keeps_more_relevant_reflection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.execute(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('routine', 'home.ac.generic', '打开空调',
                               'recall', 'owner', 'source', '打开空调',
                               1.0, ?, ?)""",
                    (now, now),
                )
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:rank', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', 'home.ac.living_room',
                               '打开空调时处理客厅设备', 'evidence', 1.0,
                               'reflection:rank', ?, ?)""",
                    (now, now),
                )

            ranked = store.rank_recalled_memories(
                [
                    MemoryRecallQuery("空调", ("home",), 0),
                    MemoryRecallQuery("客厅", ("home",), 1),
                ],
                1,
                now=now,
            )

            confirmed = next(row for row in ranked if row["source"] == "confirmed")
            reflected = next(row for row in ranked if row["source"] == "reflection")
            self.assertEqual(reflected["key"], "home.ac.living_room")
            self.assertGreater(reflected["search_score"], confirmed["search_score"])
            store.close()

    def test_ranked_memory_recall_applies_category_score_floors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.executemany(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('routine', ?, ?, 'recall', 'owner', 'source',
                               'evidence', 0.9, ?, ?)""",
                    [
                        (
                            f"confirmed.{index}",
                            f"{'空调' if index < 5 else '照明'}确认记忆{index}",
                            now,
                            now,
                        )
                        for index in range(10)
                    ],
                )
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:floor', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.executemany(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', ?, ?, 'evidence', 0.5,
                               'reflection:floor', ?, ?)""",
                    [
                        (
                            f"reflection.{index}",
                            f"{'空调' if index < 5 else '照明'}复盘记忆{index}",
                            now,
                            now,
                        )
                        for index in range(10)
                    ],
                )

            ranked = store.rank_recalled_memories(
                [MemoryRecallQuery("空调", ("home",), 0)],
                6,
                now=now,
            )

            self.assertEqual(
                len([row for row in ranked if row["source"] == "confirmed"]),
                5,
            )
            self.assertFalse(
                [row for row in ranked if row["source"] == "reflection"]
            )
            store.close()

    def test_fresh_confident_reflection_can_match_the_third_ranked_query(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:tertiary', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', 'home.ac.tertiary', '空调复盘经验',
                               'evidence', 1.0, 'reflection:tertiary', ?, ?)""",
                    (now, now),
                )

            ranked = store.rank_recalled_memories(
                [MemoryRecallQuery("空调", ("home",), 2)],
                6,
                now=now,
            )

            self.assertEqual(len(ranked), 1)
            self.assertEqual(ranked[0]["source"], "reflection")
            self.assertGreaterEqual(ranked[0]["search_score"], 0.35)
            self.assertGreaterEqual(ranked[0]["eligibility_score"], 0.35)
            store.close()

    def test_memory_query_priority_changes_rank_not_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:priority', '2030-01-01', 'completed',
                               ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', 'home.speaker.priority',
                               '小爱音箱播报方法', 'evidence', 1.0,
                               'reflection:priority', ?, ?)""",
                    (now, now),
                )

            primary = store.rank_recalled_memories(
                [MemoryRecallQuery("小爱音箱", ("home",), 0)],
                6,
                now=now,
            )
            tertiary = store.rank_recalled_memories(
                [MemoryRecallQuery("小爱音箱", ("home",), 2)],
                6,
                now=now,
            )

            self.assertEqual(len(primary), 1)
            self.assertEqual(len(tertiary), 1)
            self.assertEqual(
                primary[0]["eligibility_score"],
                tertiary[0]["eligibility_score"],
            )
            self.assertGreater(
                primary[0]["search_score"], tertiary[0]["search_score"]
            )
            store.close()

    def test_context_read_indexes_are_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            indexes = {
                str(row["name"]): str(row["sql"] or "")
                for row in store._db.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            self.assertIn("messages_turn", indexes)
            self.assertIn("messages(turn_id, id)", indexes["messages_turn"])
            self.assertIn("turns_context_recent", indexes)
            self.assertIn(
                "turns(updated_at DESC, kind, id)",
                indexes["turns_context_recent"],
            )
            self.assertIn("state<>'running'", indexes["turns_context_recent"])
            store.close()

    def test_owner_activity_update_preserves_or_resets_scene_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """UPDATE self_state SET activity='傍晚刷微博闲逛',
                   activity_result='刷到联动消息', activity_since=500,
                   last_heartbeat_at=400, next_heartbeat_at=2000 WHERE id=1"""
            )

            with patch("momoi.storage.store.time.time", return_value=900):
                store.commit_turn([], "普通回应", AgentReply([]), turn_id="unchanged")
            unchanged = store.self_state()
            self.assertEqual(unchanged["activity"], "傍晚刷微博闲逛")
            self.assertEqual(unchanged["activity_result"], "刷到联动消息")
            self.assertEqual(unchanged["activity_since"], 500)

            with patch("momoi.storage.store.time.time", return_value=1000):
                store.commit_turn(
                    [],
                    "继续聊联动",
                    AgentReply(
                        [],
                        activity_update={
                            "text": "傍晚刷微博闲逛",
                            "result": "和老师确认联动大概是日本限定",
                        },
                    ),
                    turn_id="same-activity",
                )
            continuing = store.self_state()
            self.assertEqual(continuing["activity_result"], "和老师确认联动大概是日本限定")
            self.assertEqual(continuing["activity_since"], 500)

            with patch("momoi.storage.store.time.time", return_value=1100):
                store.commit_turn(
                    [],
                    "纠正双人合作",
                    AgentReply(
                        [],
                        activity_update={
                            "text": "和老师聊清双人操控能力限制，停下今晚的合作准备",
                            "result": "双人合作推迟到 agent 能力升级以后",
                        },
                    ),
                    turn_id="new-activity",
                )
            changed = store.self_state()
            self.assertEqual(
                changed["activity"],
                "和老师聊清双人操控能力限制，停下今晚的合作准备",
            )
            self.assertEqual(changed["activity_result"], "双人合作推迟到 agent 能力升级以后")
            self.assertEqual(changed["activity_since"], 1100)
            self.assertEqual(changed["last_heartbeat_at"], 400)
            self.assertEqual(changed["next_heartbeat_at"], 2000)
            store.close()

    def test_episode_links_reject_conflicts_cycles_and_unknown_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            for episode_id in ("episode-a", "episode-b", "episode-c"):
                store.create_episode(episode_id, episode_id=episode_id)

            store.link_episodes("episode-a", "episode-b", "continues")
            store.link_episodes("episode-b", "episode-c", "supersedes")
            with self.assertRaisesRegex(ValueError, "conflicting"):
                store.link_episodes("episode-a", "episode-b", "references")
            with self.assertRaisesRegex(ValueError, "cyclic"):
                store.link_episodes("episode-c", "episode-a", "continues")
            with self.assertRaisesRegex(ValueError, "unknown"):
                store.link_episodes("episode-a", "missing", "references")
            store.close()

    def test_autonomous_episode_traversal_stops_on_legacy_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            root = store._ensure_runtime_archive(
                archive_kind="heartbeat",
                archive_day="2026-08-25",
                episode_key="legacy-cycle",
                turn_id="initial-turn",
                title="Legacy",
                now=now,
                recall_values=("initial",),
            )
            store.create_episode("Cycle peer", episode_id="cycle-peer")
            with store._db:
                store._db.executemany(
                    """INSERT INTO episode_links
                       (from_episode_id, to_episode_id, kind)
                       VALUES (?, ?, 'continues')""",
                    [("cycle-peer", root), (root, "cycle-peer")],
                )

            selected = store._ensure_runtime_archive(
                archive_kind="heartbeat",
                archive_day="2026-08-25",
                episode_key="legacy-cycle",
                turn_id="later-turn",
                title="Legacy",
                now=now + 1,
                recall_values=("later",),
            )

            self.assertIn(selected, {root, "cycle-peer"})
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM episode_turns WHERE turn_id='later-turn'"
                ).fetchone()[0],
                1,
            )
            store.close()

    def test_episode_search_uses_complete_query_phrase_without_lexical_split(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("普通消息排查", episode_id="generic-message")
            store.create_episode("真正的好消息", episode_id="good-news")
            for episode_id, turn_id, content in (
                ("generic-message", "generic-turn", "今天收到了一条普通消息"),
                ("good-news", "good-turn", "今天有好消息想讲给你听"),
            ):
                store.begin_turn(turn_id, "owner", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, 100, '[]', 'internal')""",
                        (turn_id, content),
                    )
                    store._db.execute(
                        """UPDATE turns SET state='completed', updated_at=100
                           WHERE id=?""",
                        (turn_id,),
                    )
                store.link_turn_to_episode(episode_id, turn_id)

            found = store.search_episodes("今天有好消息", 5)

            self.assertEqual([item["id"] for item in found], ["good-news"])
            self.assertEqual(
                [match["content"] for match in found[0]["matches"]],
                ["今天有好消息想讲给你听"],
            )
            store.close()

    def test_episode_time_range_matches_message_time_not_last_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("长期话题", episode_id="long-running")
            for turn_id, content, created_at in (
                ("old-turn", "七月旧暗号", 100.0),
                ("new-turn", "八月新内容", 1000.0),
            ):
                store.begin_turn(turn_id, "owner", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, '[]', 'internal')""",
                        (turn_id, content, created_at),
                    )
                    store._db.execute(
                        """UPDATE turns SET state='completed', updated_at=?
                           WHERE id=?""",
                        (created_at, turn_id),
                    )
                store.link_turn_to_episode("long-running", turn_id)
            with store._db:
                store._db.execute(
                    """UPDATE conversation_episodes
                       SET narrative_summary='八月新内容已经覆盖七月记录',
                           topics_json='[\"八月新内容\"]'
                       WHERE id='long-running'"""
                )
                store._reindex_episode_terms("long-running")

            old = store.search_episodes(
                "七月旧暗号", 5, after=50, before=150
            )
            self.assertEqual([item["id"] for item in old], ["long-running"])
            self.assertEqual(old[0]["last_activity_at"], 100)
            self.assertEqual(
                [match["content"] for match in old[0]["matches"]],
                ["七月旧暗号"],
            )
            self.assertEqual(
                store.search_episodes("八月新内容", 5, after=50, before=150),
                [],
            )
            searched = MemoryTools(store).execute(
                ToolCall(
                    "search",
                    "episode_search",
                    {
                        "query": "七月旧暗号",
                        "time_range": {
                            "kind": "range",
                            "from": "1970-01-01T00:00:50+00:00",
                            "to": "1970-01-01T00:02:30+00:00",
                        },
                    },
                ),
                [],
                TurnDraft(),
            )
            self.assertEqual(searched["results"][0]["summary_quality"], "window_matches")
            self.assertIn("七月旧暗号", searched["results"][0]["summary"])
            self.assertNotIn("八月新内容", searched["results"][0]["summary"])
            self.assertEqual(searched["results"][0]["topics"], [])
            listed = MemoryTools(store).execute(
                ToolCall(
                    "browse",
                    "episode_search",
                    {
                        "query": "",
                        "time_range": {
                            "kind": "range",
                            "from": "1970-01-01T00:00:50+00:00",
                            "to": "1970-01-01T00:02:30+00:00",
                        },
                    },
                ),
                [],
                TurnDraft(),
            )
            self.assertEqual(
                [item["id"] for item in listed["results"]], ["long-running"]
            )
            store.close()

    def test_episode_search_returns_compact_relevant_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("长期项目", episode_id="project")
            turn_id = "project-turn"
            store.begin_turn(turn_id, "owner", [turn_id])
            with store._db:
                message_id = store._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at,
                        source_event_ids_json, delivery_state)
                       VALUES (?, 'assistant', ?, 100, '[]', 'internal')""",
                    (turn_id, "紫罗兰钥匙放在书架顶层"),
                ).lastrowid
                store._db.execute(
                    """UPDATE turns SET state='completed', updated_at=100
                       WHERE id=?""",
                    (turn_id,),
                )
            store.link_turn_to_episode("project", turn_id)
            claims = [
                {
                    "message_id": int(message_id),
                    "turn_id": turn_id,
                    "ordinal": 1,
                    "role": "assistant",
                    "delivery_state": "internal",
                    "quote": "紫罗兰钥匙放在书架顶层",
                },
                *[
                    {
                        "message_id": int(message_id),
                        "turn_id": turn_id,
                        "ordinal": 1,
                        "role": "assistant",
                        "delivery_state": "internal",
                        "quote": "无关背景" + str(index),
                    }
                    for index in range(30)
                ],
            ]
            with store._db:
                store._db.execute(
                    """UPDATE conversation_episodes
                       SET working_summary=?, working_summary_claims_json=?
                       WHERE id='project'""",
                    (
                        "很长的旧摘要" * 1000,
                        json.dumps(claims, ensure_ascii=False),
                    ),
                )
                store._reindex_episode_terms("project")

            result = MemoryTools(store).execute(
                ToolCall(
                    "search",
                    "episode_search",
                    {
                        "query": "紫罗兰钥匙",
                        "time_range": {"kind": "all"},
                    },
                ),
                [],
                TurnDraft(),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 1)
            summary = result["results"][0]["summary"]
            self.assertIn("紫罗兰钥匙放在书架顶层", summary)
            self.assertLessEqual(estimate_tokens(summary), 300)
            self.assertNotIn("content", result["results"][0]["matches"][0])
            store.close()

    def test_assistant_conversation_truth_follows_delivery_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage(
                "delivery-event", "delivery-event", "告诉我结果", 1, 1
            )
            store.add_event(event)
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["这条消息等待投递"]),
                turn_id="delivery-turn",
            )
            store.create_episode("投递状态", episode_id="delivery-episode")
            store.link_turn_to_episode("delivery-episode", "delivery-turn")
            episode_id = "delivery-episode"
            outbox_id = int(
                store._db.execute(
                    "SELECT id FROM outbox WHERE turn_id='delivery-turn'"
                ).fetchone()["id"]
            )

            queued = store.conversation_episode(episode_id)["messages"][-1]
            self.assertEqual(queued["delivery_state"], "queued")
            self.assertNotIn(
                "这条消息等待投递",
                [
                    item["content"]
                    for item in store.recent_conversation_messages(1, 1000)
                ],
            )

            store.mark_ambiguous(outbox_id, 1, "timeout")
            uncertain = store.recent_conversation_messages(1, 1000)[-1]
            self.assertEqual(uncertain["delivery_state"], "uncertain")
            self.assertEqual(uncertain["content"], "这条消息等待投递")

            store.mark_failed(outbox_id, "not delivered")
            self.assertEqual(
                store.conversation_episode(episode_id)["messages"][-1][
                    "delivery_state"
                ],
                "uncertain",
            )

            failed_event = IncomingMessage(
                "failed-event", "failed-event", "再告诉我一次", 2, 2
            )
            store.add_event(failed_event)
            store.commit_turn(
                [failed_event],
                failed_event.text,
                AgentReply(["这条确定没有送达"]),
                turn_id="failed-turn",
            )
            failed_outbox_id = int(
                store._db.execute(
                    "SELECT id FROM outbox WHERE turn_id='failed-turn'"
                ).fetchone()["id"]
            )
            store.mark_failed(failed_outbox_id, "not dispatched")
            self.assertNotIn(
                "这条确定没有送达",
                [
                    item["content"]
                    for item in store.recent_conversation_messages(2, 2000)
                ],
            )
            store.close()

    def test_recent_conversation_stops_before_current_owner_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn([], "较早的聊天", AgentReply([]), turn_id="older")
            store.commit_turn([], "后来才发生的聊天", AgentReply([]), turn_id="later")
            with store._db:
                store._db.execute(
                    "UPDATE turns SET updated_at=100 WHERE id='older'"
                )
                store._db.execute(
                    "UPDATE turns SET updated_at=300 WHERE id='later'"
                )

            messages = store.recent_conversation_messages(
                10, 2000, before_timestamp=200
            )

            self.assertEqual([item["content"] for item in messages], ["较早的聊天"])
            self.assertRegex(messages[0]["timestamp"], r"^\d{4}-\d{2}-\d{2}T")
            store.close()

    def test_episode_consolidation_ignores_or_groups_unassigned_turns_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            for turn_id, text in (
                ("greeting", "早"),
                ("game-1", "昨晚开始玩湖之仆从"),
                ("game-2", "一口气打完第一章，挺开心"),
            ):
                store.commit_turn([], text, AgentReply([]), turn_id=turn_id)

            candidate = store.claim_episode_consolidation_candidate(minimum=1)
            turn_ids = [turn["turn_id"] for turn in candidate["turns"]]
            linked, deferred = store.apply_episode_consolidation(
                turn_ids,
                [
                    {
                        "action": "ignore",
                        "turn_ids": ["greeting"],
                        "reason": "ordinary greeting",
                    },
                    {
                        "action": "new",
                        "key": "lake-servant",
                        "title": "《湖之仆从》第一章",
                        "turn_ids": ["game-1", "game-2"],
                        "topics": ["湖之仆从"],
                        "entities": ["湖之仆从"],
                        "open_loops": [],
                        "salience": 0.7,
                    },
                ],
                [],
            )

            self.assertEqual(linked, 2)
            self.assertEqual(deferred, 0)
            self.assertIsNone(
                store.claim_episode_consolidation_candidate(minimum=1)
            )
            episode_id = store._db.execute(
                """SELECT episode_id FROM episode_turns
                   WHERE turn_id='game-1'"""
            ).fetchone()["episode_id"]
            self.assertEqual(
                [turn["turn_id"] for turn in store.episode_turns(episode_id)],
                ["game-1", "game-2"],
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT content FROM messages ORDER BY id"
                ).fetchall()[1]["content"],
                "昨晚开始玩湖之仆从",
            )
            store.close()

    def test_episode_consolidation_waits_for_six_eligible_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            for ordinal in range(1, 6):
                store.commit_turn(
                    [],
                    f"pending-{ordinal}",
                    AgentReply([]),
                    turn_id=f"turn-{ordinal}",
                )

            self.assertIsNone(store.claim_episode_consolidation_candidate())

            store.commit_turn(
                [], "pending-6", AgentReply([]), turn_id="turn-6"
            )
            candidate = store.claim_episode_consolidation_candidate()
            self.assertIsNotNone(candidate)
            self.assertEqual(
                [turn["turn_id"] for turn in candidate["turns"]],
                [f"turn-{ordinal}" for ordinal in range(1, 7)],
            )
            store.close()

    def test_episode_consolidation_continue_is_limited_to_supplied_candidates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("旧游戏", episode_id="old-game")
            store.commit_turn([], "继续昨晚的游戏", AgentReply([]), turn_id="turn-1")
            decision = {
                "action": "continue",
                "episode_id": "old-game",
                "turn_ids": ["turn-1"],
                "topics": [],
                "entities": [],
                "open_loops": [],
                "salience": 0.5,
            }
            with self.assertRaisesRegex(
                ValueError, "unknown consolidation episode"
            ):
                store.apply_episode_consolidation(["turn-1"], [decision], [])

            self.assertEqual(
                store.apply_episode_consolidation(
                    ["turn-1"], [decision], ["old-game"]
                ),
                (1, 0),
            )
            store.close()

    def test_runtime_archives_are_visible_but_not_owner_writable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                webhook_archive_id = store._ensure_runtime_archive(
                    archive_kind="webhook",
                    archive_day="2026-08-24",
                    episode_key="webhook:event-message:day:2026-08-24",
                    turn_id="webhook:run:0",
                    title="Webhook event-message",
                    now=now,
                    recall_values=("门锁通知",),
                )
                store._db.execute(
                    "UPDATE turns SET state='completed' WHERE id='webhook:run:0'"
                )
                heartbeat_archive_id = store._ensure_runtime_archive(
                    archive_kind="heartbeat",
                    archive_day="2026-08-25",
                    episode_key="heartbeat:day:2026-08-25",
                    turn_id="heartbeat:run:0",
                    title="心跳",
                    now=now + 1,
                    recall_values=("休息一下",),
                )
                store._db.execute(
                    "UPDATE turns SET state='completed' WHERE id='heartbeat:run:0'"
                )
                goal_archive_id = store._ensure_runtime_archive(
                    archive_kind="goal",
                    archive_day="2026-08-26",
                    episode_key="goal:night:day:2026-08-26",
                    turn_id="goal:night:0",
                    title="Goal night",
                    now=now + 2,
                    recall_values=("睡觉提醒",),
                )
                store._db.execute(
                    "UPDATE turns SET state='completed' WHERE id='goal:night:0'"
                )
            store.create_episode("普通话题", episode_id="ordinary")

            archive_ids = {webhook_archive_id, heartbeat_archive_id, goal_archive_id}
            self.assertEqual(store._runtime_archive_kind(goal_archive_id), "goal")
            self.assertEqual(store.episode(goal_archive_id)["archive_kind"], "goal")
            self.assertEqual(store.episode(goal_archive_id)["archive_day"], "2026-08-26")
            self.assertEqual(store.episode(webhook_archive_id)["archive_kind"], "webhook")
            self.assertEqual(store.episode(heartbeat_archive_id)["archive_day"], "2026-08-25")
            heartbeat_title = store.episode(heartbeat_archive_id)["title"]
            self.assertEqual(heartbeat_title, "心跳 · 2026-08-25")
            dashboard = {
                item["id"]: item["title"]
                for item in store.list_dashboard_conversations(8)
                if item["record_type"] == "episode"
            }
            self.assertEqual(
                dashboard[goal_archive_id], "Goal night · 2026-08-26"
            )
            self.assertLessEqual(
                archive_ids,
                {item["id"] for item in store.list_recent_episode_directory(8)},
            )
            self.assertTrue(
                archive_ids.isdisjoint(
                    {
                        item["id"]
                        for item in store.list_recent_episode_directory(
                            8, exclude_runtime_archives=True
                        )
                    }
                )
            )
            self.assertTrue(
                archive_ids.isdisjoint(
                    {
                        item["id"]
                        for item in store.list_episode_directory(
                            8, exclude_runtime_archives=True
                        )
                    }
                )
            )
            self.assertTrue(
                archive_ids.isdisjoint(
                    {item["id"] for item in store.open_conversation_inventory()}
                )
            )
            self.assertEqual(
                [
                    item["id"]
                    for item in store.list_recent_episode_directory(
                        1, exclude_runtime_archives=True
                    )
                ],
                ["ordinary"],
            )
            self.assertEqual(
                [
                    item["id"]
                    for item in store.list_episode_directory(
                        1, exclude_runtime_archives=True
                    )
                ],
                ["ordinary"],
            )
            self.assertEqual(
                [item["id"] for item in store.open_conversation_inventory(1)],
                ["ordinary"],
            )

            for archive_id in archive_ids:
                store.apply_conversation_actions(
                    [{"action": "close", "episode_id": archive_id}], now=now + 2
                )
                owner_turn = "owner-for-" + archive_id
                store.begin_turn(owner_turn, "owner", [owner_turn + "-event"])
                with self.assertRaisesRegex(ValueError, "archive does not accept"):
                    store.link_turn_to_episode(archive_id, owner_turn)
                self.assertEqual(store.episode(archive_id)["status"], "open")

            stale_event = IncomingMessage(
                "stale", "stale", "旧计划误选归档", now + 3, now + 3
            )
            store.add_event(stale_event)
            store.begin_turn("stale-owner", "owner", [stale_event.event_id])
            store.save_context_plan(
                "stale-owner",
                1,
                [stale_event.event_id],
                {
                    "version": 3,
                    "intent_units": [],
                    "episode_actions": [
                        {
                            "action": "continue",
                            "episode_id": heartbeat_archive_id,
                            "is_new": False,
                            "title": "Heartbeat 2026-08-25",
                            "relation": "primary",
                            "unit_ids": ["u1"],
                            "topics": ["错误话题"],
                            "entities": [],
                            "open_loops": [],
                            "salience": 0.5,
                        }
                    ],
                    "episode_links": [],
                    "uncertainty": [],
                },
            )
            with self.assertLogs("momoi.storage.conversations", level="WARNING"):
                store.commit_turn(
                    [stale_event],
                    stale_event.text,
                    AgentReply([]),
                    turn_id="stale-owner",
                )
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM episode_turns WHERE turn_id='stale-owner'"
                ).fetchone()[0],
                0,
            )
            self.assertNotIn("错误话题", store.episode(heartbeat_archive_id)["topics"])

            store.commit_turn(
                [], "这条发展成新话题", AgentReply([]), turn_id="owner-pending"
            )
            for archive_kind, archive_id in (
                ("webhook", webhook_archive_id),
                ("heartbeat", heartbeat_archive_id),
            ):
                with self.assertRaisesRegex(
                    ValueError, f"{archive_kind} archive does not accept owner turns"
                ):
                    store.link_turn_to_episode(archive_id, "owner-pending")

            store.commit_turn(
                [], "旧版本误绑的后续", AgentReply([]), turn_id="legacy-owner"
            )
            with store._db:
                store._db.execute(
                    """INSERT INTO episode_turns
                       (episode_id, turn_id, ordinal, relation, unit_ids_json)
                       VALUES (?, 'legacy-owner', 2, 'primary', '[]')""",
                    (heartbeat_archive_id,),
                )
                store._release_reply_episode_hold("legacy-owner", now + 4)
                self.assertEqual(store.episode(heartbeat_archive_id)["status"], "open")
                store._db.execute(
                    "UPDATE conversation_episodes SET status='closing' WHERE id=?",
                    (heartbeat_archive_id,),
                )
                store._apply_context_plan_episodes("unrelated", now + 5, "")
                self.assertEqual(
                    store.episode(heartbeat_archive_id)["status"], "closing"
                )

            candidate = store.claim_episode_consolidation_candidate(minimum=1)
            self.assertTrue(
                archive_ids.isdisjoint(
                    {item["id"] for item in candidate["candidate_episodes"]}
                )
            )
            self.assertEqual(candidate["context_turns"], [])
            for archive_kind, archive_id in (
                ("webhook", webhook_archive_id),
                ("heartbeat", heartbeat_archive_id),
            ):
                decision = {
                    "action": "continue",
                    "episode_id": archive_id,
                    "turn_ids": ["owner-pending"],
                    "topics": [],
                    "entities": [],
                    "open_loops": [],
                    "salience": 0.5,
                }
                with self.assertRaisesRegex(
                    ValueError,
                    f"{archive_kind} archive does not accept owner turns",
                ):
                    store.apply_episode_consolidation(
                        ["owner-pending"], [decision], [archive_id]
                    )
            store.close()

    def test_runtime_archive_kind_prefers_metadata_and_reads_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            archive_ids: dict[str, str] = {}
            with store._db:
                for index, kind in enumerate(("webhook", "heartbeat", "goal")):
                    archive_ids[kind] = store._ensure_runtime_archive(
                        archive_kind=kind,
                        archive_day="2026-09-01",
                        episode_key=f"{kind}:legacy:day:2026-09-01",
                        turn_id=f"{kind}:legacy:0",
                        title=f"{kind} legacy",
                        now=now + index,
                    )
                webhook_archive_id = archive_ids["webhook"]
                store._db.execute(
                    "UPDATE conversation_episodes SET archive_kind='goal' WHERE id=?",
                    (webhook_archive_id,),
                )
                self.assertEqual(
                    store._runtime_archive_kind(webhook_archive_id), "goal"
                )

                store._db.execute(
                    """UPDATE conversation_episodes
                       SET archive_kind=NULL, archive_day=NULL""",
                )
                for kind, archive_id in archive_ids.items():
                    with self.subTest(kind=kind):
                        self.assertEqual(store._runtime_archive_kind(archive_id), kind)

            self.assertTrue(
                set(archive_ids.values()).isdisjoint(
                    {
                        item["id"]
                        for item in store.list_recent_episode_directory(
                            8, exclude_runtime_archives=True
                        )
                    }
                )
            )
            store.close()

    def test_runtime_archive_day_uses_configured_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            timestamp = datetime(
                2026, 9, 1, 16, 30, tzinfo=ZoneInfo("UTC")
            ).timestamp()

            self.assertEqual(store._archive_day(timestamp), "2026-09-02")
            store.close()

    def test_runtime_archive_rollover_keeps_explicit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            with store._db:
                archive_id = store._ensure_runtime_archive(
                    archive_kind="webhook",
                    archive_day="2026-09-02",
                    episode_key="webhook:test:day:2026-09-02",
                    turn_id="webhook:test:0",
                    title="Webhook test",
                    now=now,
                )
                successor = store._roll_episode(
                    archive_id,
                    "webhook:test:rollover",
                    now + 1,
                    "x" * 300_000,
                )

            self.assertNotEqual(successor, archive_id)
            episode = store.episode(successor)
            self.assertEqual(episode["archive_kind"], "webhook")
            self.assertEqual(episode["archive_day"], "2026-09-02")
            store.close()

    def test_latest_consolidation_turn_is_deferred_then_reconsidered(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn([], "你在干嘛", AgentReply(["我在打游戏"]), turn_id="first")
            first_outbox = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='first'"
            ).fetchone()["id"]
            store.mark_sent(int(first_outbox))

            candidate = store.claim_episode_consolidation_candidate(minimum=1)
            self.assertEqual(
                [turn["turn_id"] for turn in candidate["turns"]], ["first"]
            )
            self.assertEqual(candidate["context_turns"], [])
            self.assertEqual(
                store.apply_episode_consolidation(
                    ["first"],
                    [
                        {
                            "action": "defer",
                            "turn_ids": ["first"],
                            "reason": "needs later context",
                        }
                    ],
                    [],
                ),
                (0, 1),
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT action FROM episode_consolidation_decisions WHERE turn_id='first'"
                ).fetchone()["action"],
                "deferred",
            )
            self.assertIsNone(
                store.claim_episode_consolidation_candidate(minimum=1)
            )

            store.commit_turn([], "在玩什么", AgentReply(["塞尔达"]), turn_id="second")
            second_outbox = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='second'"
            ).fetchone()["id"]
            store.mark_sent(int(second_outbox))
            candidate = store.claim_episode_consolidation_candidate(minimum=1)
            turn_ids = [turn["turn_id"] for turn in candidate["turns"]]
            self.assertEqual(turn_ids, ["first", "second"])
            self.assertEqual(candidate["context_turns"], [])
            self.assertEqual(
                store.apply_episode_consolidation(
                    turn_ids,
                    [
                        {
                            "action": "new",
                            "key": "playing-zelda",
                            "title": "聊正在玩的《塞尔达》",
                            "turn_ids": ["first", "second"],
                            "topics": ["游戏", "塞尔达"],
                            "entities": ["塞尔达"],
                            "open_loops": [],
                            "salience": 0.5,
                        }
                    ],
                    [],
                ),
                (2, 0),
            )
            self.assertIsNone(
                store.claim_episode_consolidation_candidate(minimum=1)
            )
            store.close()

    def test_deferred_turn_reconsiders_with_already_linked_later_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn([], "你在干嘛", AgentReply(["我在打游戏"]), turn_id="first")
            first_outbox = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='first'"
            ).fetchone()["id"]
            store.mark_sent(int(first_outbox))
            store.apply_episode_consolidation(
                ["first"],
                [
                    {
                        "action": "defer",
                        "turn_ids": ["first"],
                        "reason": "needs later context",
                    }
                ],
                [],
            )

            store.commit_turn([], "在玩什么", AgentReply(["塞尔达"]), turn_id="second")
            second_outbox = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='second'"
            ).fetchone()["id"]
            store.mark_sent(int(second_outbox))
            store.create_episode("聊正在玩的游戏", episode_id="playing-game")
            store.link_turn_to_episode("playing-game", "second")

            candidate = store.claim_episode_consolidation_candidate(minimum=1)
            self.assertEqual(
                [turn["turn_id"] for turn in candidate["turns"]], ["first"]
            )
            self.assertEqual(
                [turn["turn_id"] for turn in candidate["context_turns"]],
                ["second"],
            )
            self.assertEqual(
                candidate["context_turns"][0]["episode_id"], "playing-game"
            )
            self.assertIn(
                "playing-game",
                {episode["id"] for episode in candidate["candidate_episodes"]},
            )
            self.assertEqual(
                store.apply_episode_consolidation(
                    ["first"],
                    [
                        {
                            "action": "continue",
                            "episode_id": "playing-game",
                            "turn_ids": ["first"],
                            "topics": ["游戏"],
                            "entities": ["塞尔达"],
                            "open_loops": [],
                            "salience": 0.5,
                        }
                    ],
                    ["playing-game"],
                ),
                (1, 0),
            )
            self.assertEqual(
                [
                    (turn["turn_id"], turn["ordinal"])
                    for turn in store.episode_turns("playing-game")
                ],
                [("first", 1), ("second", 2)],
            )
            self.assertIsNone(
                store.claim_episode_consolidation_candidate(minimum=1)
            )
            store.close()

    def test_deferred_latest_may_be_ignored_when_later_context_exists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn([], "ping", AgentReply([]), turn_id="ping")
            store.apply_episode_consolidation(
                ["ping"],
                [
                    {
                        "action": "defer",
                        "turn_ids": ["ping"],
                        "reason": "needs later context",
                    }
                ],
                [],
            )
            store.commit_turn([], "去玩吧", AgentReply(["好"]), turn_id="later")
            later_outbox = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='later'"
            ).fetchone()["id"]
            store.mark_sent(int(later_outbox))
            store.create_episode("休息去玩", episode_id="play")
            store.link_turn_to_episode("play", "later")

            self.assertEqual(
                store.apply_episode_consolidation(
                    ["ping"],
                    [
                        {
                            "action": "ignore",
                            "turn_ids": ["ping"],
                            "reason": "later context shows isolated ping",
                        }
                    ],
                    ["play"],
                    allow_ignore_latest=True,
                ),
                (0, 0),
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT action FROM episode_consolidation_decisions WHERE turn_id='ping'"
                ).fetchone()["action"],
                "ignored",
            )
            self.assertIsNone(
                store.claim_episode_consolidation_candidate(minimum=1)
            )
            store.close()

    def test_latest_consolidation_turn_cannot_be_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn([], "你好", AgentReply(["你好"]), turn_id="greeting")
            outbox_id = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='greeting'"
            ).fetchone()["id"]
            store.mark_sent(int(outbox_id))

            with self.assertRaisesRegex(
                ValueError, "latest consolidation turn may not be ignored"
            ):
                store.apply_episode_consolidation(
                    ["greeting"],
                    [
                        {
                            "action": "ignore",
                            "turn_ids": ["greeting"],
                            "reason": "ordinary greeting",
                        }
                    ],
                    [],
                )
            self.assertIsNotNone(
                store.claim_episode_consolidation_candidate(minimum=1)
            )
            store.close()

    def test_episode_consolidation_skips_completed_turns_without_messages(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("empty", "owner", ["empty-event"])
            store.complete_background_turn("empty")
            store.commit_turn([], "真实对话", AgentReply([]), turn_id="real")

            candidate = store.claim_episode_consolidation_candidate(minimum=1)

            self.assertEqual(
                [turn["turn_id"] for turn in candidate["turns"]],
                ["real"],
            )
            store.close()

    def test_consolidation_backfill_reorders_episode_and_invalidates_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [], "你在干嘛", AgentReply(["我在打游戏"]), turn_id="earlier"
            )
            earlier_outbox = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='earlier'"
            ).fetchone()["id"]
            store.mark_sent(int(earlier_outbox))
            store.commit_turn(
                [], "在玩什么", AgentReply(["塞尔达"]), turn_id="later"
            )
            later_outbox = store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='later'"
            ).fetchone()["id"]
            store.mark_sent(int(later_outbox))
            store.create_episode("聊正在玩的游戏", episode_id="playing-game")
            store.link_turn_to_episode("playing-game", "later")
            with store._db:
                store._db.execute(
                    """UPDATE conversation_episodes
                       SET working_summary='old', working_summary_claims_json='[{}]',
                           narrative_summary='old narrative',
                           emotional_context_json='{"tone":"old"}',
                           outcomes_json='["old"]',
                           summarized_through_ordinal=1
                       WHERE id='playing-game'"""
                )

            self.assertEqual(
                store.apply_episode_consolidation(
                    ["earlier"],
                    [
                        {
                            "action": "continue",
                            "episode_id": "playing-game",
                            "turn_ids": ["earlier"],
                            "topics": ["游戏"],
                            "entities": ["塞尔达"],
                            "open_loops": [],
                            "salience": 0.5,
                        }
                    ],
                    ["playing-game"],
                ),
                (1, 0),
            )

            self.assertEqual(
                [
                    (turn["turn_id"], turn["ordinal"])
                    for turn in store.episode_turns("playing-game")
                ],
                [("earlier", 1), ("later", 2)],
            )
            episode = store.episode("playing-game")
            self.assertEqual(episode["working_summary"], "")
            self.assertEqual(episode["working_summary_claims"], [])
            self.assertEqual(episode["narrative_summary"], "")
            self.assertEqual(episode["emotional_context"], {})
            self.assertEqual(episode["outcomes"], [])
            self.assertEqual(episode["summarized_through_ordinal"], 0)
            store.close()

    def test_episode_rolls_to_successor_after_turn_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode(
                "长期项目",
                episode_id="long-project",
                topics=["项目"],
            )
            for ordinal in range(64):
                turn_id = f"old-{ordinal}"
                store.begin_turn(turn_id, "owner", [turn_id])
                store.complete_background_turn(turn_id)
                store.link_turn_to_episode("long-project", turn_id)

            event = IncomingMessage("next", "next", "继续这个项目", 1, 1)
            store.add_event(event)
            store.begin_turn("next", "owner", [event.event_id])
            store.save_context_plan(
                "next",
                1,
                [event.event_id],
                {
                    "version": 2,
                    "intent_units": [
                        {
                            "id": "u1",
                            "event_ids": [event.event_id],
                            "text": event.text,
                            "intent": "continue project",
                            "speech_act": "casual_share",
                            "references": [],
                            "recall_queries": [],
                        }
                    ],
                    "episode_actions": [
                        {
                            "action": "continue",
                            "episode_id": "long-project",
                            "is_new": False,
                            "title": "长期项目",
                            "relation": "primary",
                            "unit_ids": ["u1"],
                            "topics": ["新阶段"],
                            "entities": [],
                            "open_loops": [],
                            "salience": 0.6,
                        }
                    ],
                    "episode_links": [],
                    "uncertainty": [],
                },
            )
            store.commit_turn(
                [event], event.text, AgentReply([]), turn_id="next"
            )

            successor = store._db.execute(
                """SELECT from_episode_id FROM episode_links
                   WHERE to_episode_id='long-project' AND kind='continues'"""
            ).fetchone()["from_episode_id"]
            self.assertEqual(store.episode("long-project")["status"], "closed")
            self.assertEqual(store.episode(successor)["title"], "长期项目")
            self.assertEqual(
                [turn["turn_id"] for turn in store.episode_turns(successor)],
                ["next"],
            )
            store.close()

    def test_episode_metadata_merges_and_closing_episodes_age_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")

            def commit(turn_id: str, episode_id: str, topic: str) -> None:
                event = IncomingMessage(turn_id, turn_id, f"处理 {turn_id}", 1, 1)
                store.add_event(event)
                store.begin_turn(turn_id, "owner", [event.event_id])
                context_plan = {
                    "version": 1,
                    "intent_units": [
                        {
                            "id": "u1",
                            "event_ids": [event.event_id],
                            "text": event.text,
                            "intent": "continue topic",
                            "references": [],
                            "recall_queries": [event.text],
                        }
                    ],
                    "episode_actions": [
                        {
                            "action": (
                                "new" if store.episode(episode_id) is None else "continue"
                            ),
                            "episode_id": episode_id,
                            "is_new": store.episode(episode_id) is None,
                            "title": f"主题 {episode_id}",
                            "relation": "primary",
                            "unit_ids": ["u1"],
                            "topics": [topic],
                            "entities": [],
                            "open_loops": [],
                            "salience": 0.5,
                        }
                    ],
                    "episode_links": [],
                    "uncertainty": [],
                }
                store.save_context_plan(turn_id, 1, [event.event_id], context_plan)
                store.commit_turn(
                    [event], event.text, AgentReply(["收到"]), turn_id=turn_id
                )

            commit("turn-a1", "episode-a", "obsolete_marker")
            commit("turn-a2", "episode-a", "fresh_marker")
            self.assertEqual(
                store.episode("episode-a")["topics"],
                ["obsolete_marker", "fresh_marker"],
            )
            self.assertEqual(
                [item["id"] for item in store.search_episodes("obsolete_marker", 3)],
                ["episode-a"],
            )

            commit("turn-b", "episode-b", "第二主题")
            self.assertEqual(store.episode("episode-a")["status"], "closed")
            commit("turn-c", "episode-c", "第三主题")
            self.assertEqual(store.episode("episode-a")["status"], "closed")
            self.assertEqual(store.episode("episode-b")["status"], "closed")
            self.assertEqual(
                [item["id"] for item in store.open_conversation_inventory()],
                ["episode-c"],
            )
            store.close()

    def test_goal_schedule_contract_is_shared_and_precise(self) -> None:
        schedules = [
            item["input_schema"]["properties"]["schedule"]
            for item in AGENDA_TOOL_SPECS
            if item["name"] in {"goal_create", "goal_update"}
        ]
        self.assertEqual(
            [schedule["oneOf"] for schedule in schedules],
            [schedules[0]["oneOf"]] * 2,
        )
        self.assertEqual(len({id(schedule) for schedule in schedules}), 2)
        daily = schedules[0]["oneOf"][1]
        self.assertIn("times", daily["properties"])
        self.assertNotIn("at", daily["properties"])
        self.assertEqual(daily["required"], ["kind", "times"])
        self.assertNotIn("reminder_create", {item["name"] for item in AGENDA_TOOL_SPECS})
        self.assertIn("Use a Goal for every future action", AGENDA_TOOL_POLICY)
        self.assertIn("governed", AGENDA_TOOL_POLICY)
        self.assertIn("by the shared Style Card", AGENDA_TOOL_POLICY)
        self.assertIn("judgment, not a reflex", MEMORY_TOOL_POLICY)
        self.assertIn(
            "never persist a more specific claim than the exact owner quote entails",
            MEMORY_TOOL_POLICY,
        )
        self.assertIn("locate the committed memory", MEMORY_TOOL_POLICY)
        self.assertIn("native transcript tool", MEMORY_TOOL_POLICY)
        self.assertIn("replace_confirmed=true", MEMORY_TOOL_POLICY)
        self.assertIn("this is `recent`, never", MEMORY_TOOL_POLICY)
        self.assertIn("A procedure you are afraid of forgetting is not `always`", MEMORY_TOOL_POLICY)
        remember = next(
            spec for spec in MEMORY_TOOL_SPECS if spec["name"] == "memory_remember"
        )
        self.assertNotIn("durable", remember["description"])
        self.assertEqual(
            remember["input_schema"]["properties"]["activation"]["enum"],
            ["recall", "recent", "always"],
        )
        self.assertIn(
            "Do not turn 这个/this into a standing rule",
            remember["input_schema"]["properties"]["content"]["description"],
        )
        forget = next(
            spec for spec in MEMORY_TOOL_SPECS if spec["name"] == "memory_forget"
        )
        self.assertIn("directly disconfirmed", forget["description"])

    def test_context_plan_revisions_and_episode_turn_links_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store.begin_turn("turn-1", "owner", ["event-1"])
            store.begin_turn("turn-2", "owner", ["event-2"])
            store.begin_turn("turn-3", "owner", ["event-3"])

            first = store.save_context_plan(
                "turn-1",
                1,
                ["event-1"],
                {"units": [{"id": "unit-1", "query": "邮件"}]},
            )
            self.assertEqual(first["state"], "planned")
            recalled = store.save_context_retrieval(
                "turn-1", 1, {"version": 6, "memory_ids": [7]}
            )
            self.assertEqual(recalled["state"], "recalled")
            self.assertEqual(recalled["retrieval"]["memory_ids"], [7])
            self.assertEqual(recalled["retrieval"]["version"], 6)
            second = store.save_context_plan(
                "turn-1",
                2,
                ["event-1", "event-update"],
                {"units": [{"id": "unit-2", "query": "邮件和微博"}]},
            )
            self.assertEqual(second["revision"], 2)
            self.assertEqual(store.context_plan("turn-1", 1)["state"], "superseded")
            self.assertEqual(store.context_plan("turn-1")["revision"], 2)

            mail = store.create_episode(
                "邮件跟进", episode_id="episode-mail", topics=["邮件"], salience=0.8
            )
            store.create_episode(
                "微博浏览", episode_id="episode-social", topics=["微博"]
            )
            self.assertEqual(mail["topics"], ["邮件"])
            self.assertEqual(
                {item["id"] for item in store.open_conversation_inventory()},
                {"episode-mail", "episode-social"},
            )
            first_link = store.link_turn_to_episode(
                "episode-mail", "turn-1", unit_ids=["unit-1"]
            )
            related_link = store.link_turn_to_episode(
                "episode-social",
                "turn-1",
                relation="related",
                unit_ids=["unit-2"],
            )
            second_link = store.link_turn_to_episode(
                "episode-mail", "turn-2", unit_ids=["unit-3"]
            )
            self.assertEqual(first_link["ordinal"], 1)
            self.assertEqual(related_link["ordinal"], 1)
            self.assertEqual(second_link["ordinal"], 2)
            self.assertEqual(
                [item["turn_id"] for item in store.episode_turns("episode-mail")],
                ["turn-1", "turn-2"],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store._db.execute(
                    """INSERT INTO episode_turns
                       (episode_id, turn_id, ordinal, relation, unit_ids_json)
                       VALUES ('episode-mail', 'turn-3', 2, 'related', '[]')"""
                )
            store._db.rollback()
            store.link_episodes("episode-social", "episode-mail", "references")
            store.close()

            reopened = Store(path)
            self.assertEqual(reopened.context_plan("turn-1", 2)["state"], "planned")
            self.assertEqual(
                [item["ordinal"] for item in reopened.episode_turns("episode-mail")],
                [1, 2],
            )
            self.assertEqual(
                reopened._db.execute("SELECT COUNT(*) FROM episode_links").fetchone()[
                    0
                ],
                1,
            )
            reopened.close()

    def test_existing_database_adds_runtime_archive_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store.create_episode("Legacy episode", episode_id="legacy")
            store.close()

            database = sqlite3.connect(path)
            database.execute(
                "ALTER TABLE conversation_episodes DROP COLUMN archive_kind"
            )
            database.execute(
                "ALTER TABLE conversation_episodes DROP COLUMN archive_day"
            )
            database.execute("PRAGMA user_version=0")
            database.commit()
            database.close()

            reopened = Store(path)
            columns = {
                str(row[1])
                for row in reopened._db.execute(
                    "PRAGMA table_info(conversation_episodes)"
                )
            }
            self.assertLessEqual({"archive_kind", "archive_day"}, columns)
            legacy = reopened.episode("legacy")
            self.assertEqual(legacy["title"], "Legacy episode")
            self.assertIsNone(legacy["archive_kind"])

            with reopened._db:
                archive_id = reopened._ensure_runtime_archive(
                    archive_kind="heartbeat",
                    archive_day="2026-09-02",
                    episode_key="heartbeat:day:2026-09-02",
                    turn_id="heartbeat:migration-test",
                    title="心跳",
                    now=time.time(),
                )
            archive = reopened.episode(archive_id)
            self.assertEqual(archive["archive_kind"], "heartbeat")
            self.assertEqual(archive["archive_day"], "2026-09-02")
            reopened.close()

    def test_database_migrations_are_versioned_and_reject_newer_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            version = int(store._db.execute("PRAGMA user_version").fetchone()[0])
            self.assertGreater(version, 0)
            store.close()

            database = sqlite3.connect(path)
            database.execute(f"PRAGMA user_version={version + 1}")
            database.close()

            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                Store(path)

    def test_turn_workflow_is_explicit_with_one_legacy_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("current", "heartbeat", ["goal:misleading"])
            current = store._db.execute(
                "SELECT kind, workflow_kind FROM turns WHERE id='current'"
            ).fetchone()
            self.assertEqual(tuple(current), ("autonomous", "heartbeat"))
            self.assertEqual(store.turn_workflow_kind("current"), "heartbeat")

            with store._db:
                store._db.execute(
                    """INSERT INTO turns
                       (id, kind, source_ids_json, state, started_at, updated_at)
                       VALUES ('legacy', 'autonomous', '["reply-followup:1"]',
                               'completed', 1, 1)"""
                )
                store._db.execute(
                    """INSERT INTO turns
                       (id, kind, source_ids_json, state, stage,
                        started_at, updated_at)
                       VALUES ('legacy-reflection', 'autonomous',
                               '["reflection:2026-09-01"]', 'completed',
                               'completed', 1, 1),
                              ('legacy-memory-maintenance', 'autonomous',
                               '["reflection:2026-09-01"]', 'completed',
                               'completed', 1, 1)"""
                )
                store._db.execute(
                    """INSERT INTO turn_journal
                       (turn_id, sequence, created_at, item_type,
                        visibility, trust, payload_json)
                       VALUES ('legacy-memory-maintenance', 1, 1,
                               'memory_maintenance_complete',
                               'internal', 'runtime', '{}')"""
                )
            self.assertEqual(store.turn_workflow_kind("legacy"), "reply_followup")
            self.assertEqual(
                store.turn_workflow_kind("legacy-reflection"), "reflection"
            )
            self.assertEqual(
                store.turn_workflow_kind("legacy-memory-maintenance"),
                "memory_maintenance",
            )

            with self.assertRaisesRegex(ValueError, "belongs to heartbeat"):
                store.begin_turn("current", "goal", ["goal:misleading"])
            with self.assertRaisesRegex(ValueError, "unknown Turn workflow"):
                store.begin_turn("invalid", "autonomous", ["invalid"])
            store.close()

    def test_corrupt_internal_json_is_reported_and_maintenance_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("损坏测试", episode_id="corrupt-episode")
            with store._db:
                store._db.execute(
                    """UPDATE conversation_episodes
                       SET working_summary_claims_json='not-json'
                       WHERE id='corrupt-episode'"""
                )
            with self.assertLogs("momoi.storage.integrity", level="ERROR") as logs:
                episode = store.episode("corrupt-episode")
            self.assertEqual(episode["working_summary_claims"], [])
            self.assertEqual(logs.records[0].momoi_event, "storage_integrity_error")
            self.assertEqual(
                logs.records[0].momoi_fields["field"],
                "working_summary_claims_json",
            )

            store.queue_memory_maintenance_turn("corrupt-maintenance", "reflection:1")
            with store._db:
                store._db.execute(
                    """INSERT INTO turn_journal
                       (turn_id, sequence, created_at, item_type, visibility,
                        trust, payload_json)
                       VALUES ('corrupt-maintenance', 1, 1,
                               'memory_maintenance_plan', 'internal', 'runtime',
                               'not-json')"""
                )
            with self.assertLogs("momoi.storage.integrity", level="ERROR"):
                with self.assertRaises(StorageIntegrityError):
                    store.memory_maintenance_journal("corrupt-maintenance")
            turn = store._db.execute(
                "SELECT state, failure_reason FROM turns WHERE id='corrupt-maintenance'"
            ).fetchone()
            self.assertEqual(turn["state"], "needs_reconciliation")
            self.assertIn("storage_integrity_error", turn["failure_reason"])
            store.close()

    def test_recall_reuse_candidate_is_only_the_latest_effective_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("recalled-turn", "owner", ["event-1"])
            store.begin_turn("planned-turn", "owner", ["event-2"])
            store.begin_turn("missed-turn", "owner", ["event-3"])
            store.begin_turn("reuse-turn", "owner", ["event-4"])
            store.create_episode("摩旅计划", episode_id="trip")
            recalled_plan = {
                "intent_units": [
                    {
                        "id": "u1",
                        "recall": {"mode": "search", "queries": ["长寿湖"]},
                        "recall_queries": ["长寿湖"],
                        "recall_from_turn_id": "",
                    }
                ]
            }
            store.save_context_plan(
                "recalled-turn", 1, ["event-1"], recalled_plan
            )
            store.save_context_retrieval(
                "recalled-turn",
                1,
                {
                    "version": 4,
                    "recall_memories": [],
                    "reflection_memories": [],
                    "query_recall": "queries=长寿湖\nhits=长寿湖",
                },
            )
            store.link_turn_to_episode("trip", "recalled-turn", unit_ids=["u1"])
            store.save_context_plan(
                "planned-turn", 1, ["event-2"], recalled_plan
            )
            store.link_turn_to_episode("trip", "planned-turn", unit_ids=["u1"])
            store.save_context_plan(
                "missed-turn", 1, ["event-3"], recalled_plan
            )
            store.save_context_retrieval(
                "missed-turn",
                1,
                {
                    "version": 4,
                    "recall_memories": [],
                    "reflection_memories": [],
                    "query_recall": "queries=长寿湖\nmisses=长寿湖",
                },
            )
            store.link_turn_to_episode("trip", "missed-turn", unit_ids=["u1"])
            reuse_plan = {
                "intent_units": [
                    {
                        "id": "u1",
                        "recall": {
                            "mode": "reuse",
                            "from_turn_id": "recalled-turn",
                        },
                        "recall_queries": [],
                        "recall_from_turn_id": "recalled-turn",
                    }
                ]
            }
            store.save_context_plan("reuse-turn", 1, ["event-4"], reuse_plan)
            store.save_context_retrieval(
                "reuse-turn",
                1,
                {
                    "version": 4,
                    "recall_memories": [],
                    "reflection_memories": [],
                    "query_recall": "reused_from=recalled-turn units=u1",
                },
            )

            candidates = store.recall_reuse_candidates(
                [
                    "recalled-turn",
                    "planned-turn",
                    "missed-turn",
                    "missing-turn",
                    "reuse-turn",
                ]
            )

            self.assertEqual(
                candidates,
                [{"turn_id": "reuse-turn", "queries": ["长寿湖"]}],
            )
            store.close()

    def test_episode_read_pages_back_through_a_long_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("很长的旧对话", episode_id="long-episode")
            for ordinal in range(1, 5):
                turn_id = f"long-turn-{ordinal}"
                store.begin_turn(turn_id, "owner", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at, source_event_ids_json)
                           VALUES (?, 'assistant', ?, ?, '[]')""",
                        (turn_id, f"第{ordinal}页" + "细节" * 5000, ordinal),
                    )
                store.link_turn_to_episode("long-episode", turn_id)

            first = MemoryTools(store).execute(
                ToolCall(
                    "read-newest",
                    "episode_read",
                    {"episode_id": "long-episode"},
                ),
                [],
                TurnDraft(),
            )["episode"]
            self.assertTrue(first["truncated"])
            self.assertIsNotNone(first["next_before_ordinal"])
            second = MemoryTools(store).execute(
                ToolCall(
                    "read-older",
                    "episode_read",
                    {
                        "episode_id": "long-episode",
                        "before_ordinal": first["next_before_ordinal"],
                    },
                ),
                [],
                TurnDraft(),
            )["episode"]
            first_ordinals = {item["ordinal"] for item in first["messages"]}
            second_ordinals = {item["ordinal"] for item in second["messages"]}
            self.assertFalse(first_ordinals & second_ordinals)
            self.assertEqual(first_ordinals | second_ordinals, {1, 2, 3, 4})
            self.assertIsNone(second["next_before_ordinal"])
            store.close()

    def test_episode_read_filters_raw_messages_by_exact_time_range(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("跨时段对话", episode_id="time-window")
            for turn_id, content, created_at in (
                ("before", "窗口之前", 100.0),
                ("inside-1", "窗口内第一条", 200.0),
                ("inside-2", "窗口内第二条", 250.0),
                ("after", "窗口之后", 400.0),
            ):
                store.begin_turn(turn_id, "owner", [turn_id])
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, '[]', 'internal')""",
                        (turn_id, content, created_at),
                    )
                store.link_turn_to_episode("time-window", turn_id)

            result = MemoryTools(store).execute(
                ToolCall(
                    "read-window",
                    "episode_read",
                    {
                        "episode_id": "time-window",
                        "time_range": {
                            "kind": "range",
                            "from": "1970-01-01T00:03:00+00:00",
                            "to": "1970-01-01T00:05:00+00:00",
                        },
                    },
                ),
                [],
                TurnDraft(),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["time_range"]["kind"], "range")
            self.assertEqual(
                [message["content"] for message in result["episode"]["messages"]],
                ["窗口内第一条", "窗口内第二条"],
            )
            self.assertFalse(result["episode"]["truncated"])
            self.assertIsNone(result["episode"]["next_before_ordinal"])
            self.assertEqual(
                result["episode"]["window_first_timestamp"],
                result["episode"]["messages"][0]["timestamp"],
            )
            self.assertEqual(
                result["episode"]["window_last_timestamp"],
                result["episode"]["messages"][-1]["timestamp"],
            )
            store.close()

    def test_episode_read_schema_describes_episode_turn_messages(self) -> None:
        spec = next(
            spec for spec in MEMORY_TOOL_SPECS if spec["name"] == "episode_read"
        )
        self.assertIn("Episode id", spec["description"])
        self.assertIn("turn_id", spec["description"])
        self.assertIn("Episode ordinal", spec["description"])
        self.assertIn(
            "must be used cautiously",
            spec["input_schema"]["properties"]["time_range"]["description"],
        )

    def test_episode_read_continues_inside_one_oversized_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("单条超长消息", episode_id="oversized")
            turn_id = "oversized-turn"
            store.begin_turn(turn_id, "owner", [turn_id])
            secret = "末尾仍然必须可以读取"
            with store._db:
                store._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json)
                       VALUES (?, 'assistant', ?, 1, '[]')""",
                    (turn_id, "甲" * 35000 + secret),
                )
            store.link_turn_to_episode("oversized", turn_id)

            first = MemoryTools(store).execute(
                ToolCall("first", "episode_read", {"episode_id": "oversized"}),
                [],
                TurnDraft(),
            )["episode"]["messages"][0]
            self.assertIsNotNone(first["next_content_offset"])
            second = MemoryTools(store).execute(
                ToolCall(
                    "second",
                    "episode_read",
                    {
                        "episode_id": "oversized",
                        "message_id": first["id"],
                        "content_offset": first["next_content_offset"],
                    },
                ),
                [],
                TurnDraft(),
            )["message"]
            self.assertIn(secret, second["content"])
            self.assertIsNone(second["next_content_offset"])
            store.close()

    def test_recoverable_failed_emotion_is_requeued_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            emotion = workspace / "emotion"
            emotion.mkdir(parents=True)
            asset = emotion / "asset.gif"
            asset.write_bytes(b"gif")
            database = workspace / "data" / "momoi.sqlite3"
            database.parent.mkdir()
            stored_path = "emotion/asset.gif"

            store = Store(database, workspace)
            now = time.time()
            store._db.execute(
                """INSERT INTO emotions(slug, path, description, created_at, updated_at)
                   VALUES ('salute', ?, '敬礼', ?, ?)""",
                (stored_path, now, now),
            )
            payload = json.dumps(
                {
                    "action": "message",
                    "segments": [
                        {"type": "image", "data": {"file": stored_path}}
                    ],
                }
            )
            store._db.execute(
                """INSERT INTO outbox
                   (turn_id, dedupe_key, text, state, attempts, last_error,
                    kind, media_path, payload_json)
                   VALUES ('turn', 'emotion', 'emotion://salute', 'failed', 1,
                           'media asset cannot be read: FileNotFoundError',
                           'image', ?, ?)""",
                (stored_path, payload),
            )
            store._db.commit()
            store.close()

            reopened = Store(database, workspace)
            raw_outbox = reopened._db.execute(
                "SELECT state, media_path, payload_json FROM outbox WHERE dedupe_key='emotion'"
            ).fetchone()
            self.assertEqual(raw_outbox["media_path"], "emotion/asset.gif")
            self.assertIn("emotion/asset.gif", raw_outbox["payload_json"])
            self.assertEqual(raw_outbox["state"], "pending")
            due = reopened.due_outbox()[0]
            self.assertEqual(due.media_path, str(asset.resolve()))
            self.assertEqual(
                due.payload["segments"][0]["data"]["file"], str(asset.resolve())
            )
            reopened.close()

    def test_tool_audit_reuses_completed_call_and_blocks_ambiguous_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            self.assertIsNone(
                store.begin_tool_call("turn-1", "call-1", "curl", {"url": "http://x"})
            )
            store.complete_tool_call("turn-1", "call-1", {"ok": True, "status": 200})
            self.assertEqual(
                store.begin_tool_call("turn-1", "call-1", "curl", {"url": "http://x"}),
                {"ok": True, "status": 200},
            )
            self.assertEqual(
                store.begin_tool_call("turn-1", "call-1", "curl", {"url": "http://y"})[
                    "error"
                ],
                "tool_call_id_conflict",
            )
            self.assertIsNone(
                store.begin_tool_call("turn-1", "call-2", "write_file", {"path": "/x"})
            )
            replay = store.begin_tool_call(
                "turn-1", "call-2", "write_file", {"path": "/x"}
            )
            self.assertTrue(replay["ambiguous"])
            store.close()

    def test_crashed_external_effect_turn_requires_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            self.assertEqual(
                store.begin_turn("turn-crash", "owner", ["qq:1:crash"]),
                "running",
            )
            self.assertIsNone(
                store.begin_tool_call(
                    "turn-crash", "call-crash", "curl", {"url": "http://x"}
                )
            )
            store.close()

            recovered = Store(path)
            self.assertEqual(
                recovered.begin_turn("turn-crash", "owner", ["qq:1:crash"]),
                "needs_reconciliation",
            )
            turn = recovered._db.execute(
                "SELECT stage, failure_reason FROM turns WHERE id='turn-crash'"
            ).fetchone()
            self.assertEqual(turn["stage"], "needs_reconciliation")
            self.assertEqual(
                turn["failure_reason"],
                "process_interrupted_after_external_effect",
            )
            self.assertEqual(
                recovered._db.execute(
                    "SELECT status FROM reconciliations WHERE turn_id='turn-crash'"
                ).fetchone()["status"],
                "open",
            )
            recovered.close()

    def test_read_only_tool_crash_does_not_require_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store.begin_turn("turn-read", "owner", ["qq:1:read"])
            self.assertIsNone(
                store.begin_tool_call(
                    "turn-read",
                    "call-read",
                    "read_file",
                    {"path": "/tmp/read-only"},
                    "read",
                )
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT capability FROM tool_audit WHERE turn_id='turn-read'"
                ).fetchone()[0],
                "read",
            )
            store.record_turn_failure("turn-read", "ProviderError")
            store.record_turn_usage("turn-read", 100, 20)
            store._db.execute(
                "UPDATE turns SET stage='llm', started_at=1 WHERE id='turn-read'"
            )
            store._db.commit()
            store.close()

            recovered = Store(path)
            before_retry = time.time()
            self.assertEqual(
                recovered.begin_turn("turn-read", "owner", ["qq:1:read"]),
                "running",
            )
            turn = recovered._db.execute(
                """SELECT stage, failure_reason, llm_calls, input_tokens,
                          output_tokens, started_at
                   FROM turns WHERE id='turn-read'"""
            ).fetchone()
            self.assertEqual(turn["stage"], "started")
            self.assertIsNone(turn["failure_reason"])
            self.assertEqual(turn["llm_calls"], 0)
            self.assertEqual(turn["input_tokens"], 0)
            self.assertEqual(turn["output_tokens"], 0)
            self.assertGreaterEqual(turn["started_at"], before_retry)
            self.assertEqual(
                recovered._db.execute(
                    "SELECT COUNT(*) FROM reconciliations WHERE status='open'"
                ).fetchone()[0],
                0,
            )
            recovered.close()

        self.assertEqual(
            BuiltinTools.capability(ToolCall("get", "curl", {"method": "GET"})),
            "read",
        )
        self.assertEqual(
            BuiltinTools.capability(ToolCall("post", "curl", {"method": "POST"})),
            "external_effect",
        )
        self.assertEqual(
            BuiltinTools.capability(ToolCall("write", "write_file", {})),
            "write",
        )
        self.assertEqual(
            BuiltinTools.capability(ToolCall("list", "list_dir", {})),
            "read",
        )
        for name in ("makedirs", "move_file", "delete_file"):
            self.assertEqual(
                BuiltinTools.capability(ToolCall(name, name, {})),
                "write",
            )

    def test_progress_message_crash_reuses_outbox_without_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store.begin_turn("turn-progress", "owner", ["qq:1:progress"])
            store.queue_progress(
                "turn-progress", "progress-1", ["先说这一句"], "napcat"
            )
            store.close()

            recovered = Store(path)
            self.assertEqual(
                recovered.begin_turn("turn-progress", "owner", ["qq:1:progress"]),
                "running",
            )
            recovered.queue_progress(
                "turn-progress", "progress-1", ["先说这一句"], "napcat"
            )
            self.assertEqual(
                recovered._db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE turn_id='turn-progress'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                recovered._db.execute(
                    "SELECT COUNT(*) FROM reconciliations WHERE status='open'"
                ).fetchone()[0],
                0,
            )
            recovered.close()

    def test_owner_can_resolve_or_resume_open_reconciliation_by_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            turn_id = "a" * 32
            daemon.store.open_reconciliation(turn_id, "unknown_external_result")
            command = IncomingMessage(
                "qq:1:resume",
                "resume",
                f"/resume {turn_id[:12]} 设备确认没有打开，请继续",
                1,
                1,
            )
            result = daemon._apply_reconciliation_commands([command])
            self.assertIn("status=resumed", result)
            self.assertIn("设备确认没有打开，请继续", result)
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT COUNT(*) FROM reconciliations WHERE status='open'"
                ).fetchone()[0],
                0,
            )
            with self.assertRaisesRegex(ValueError, "not found"):
                daemon.store.resolve_reconciliation(
                    turn_id[:12], "重复确认", resume=False
                )
            daemon.store.close()

    def test_goal_is_persisted_claimed_and_rescheduled_with_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage("qq:1:goal", "goal", "十分钟后继续检查", 1, 1)
            store.add_event(event)
            draft = TurnDraft()
            created = tools.execute(
                ToolCall(
                    "goal-create",
                    "goal_create",
                    {
                        "title": "检查任务",
                        "success_criteria": "确认检查完成",
                        "next_action": "再次检查",
                        "next_review_at": (
                            datetime.now(ZoneInfo("UTC")) + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )
            self.assertTrue(created["ok"])
            goal_id = created["goal"]["id"]
            store.commit_turn([event], event.text, AgentReply(["接下了"]), draft)
            time.sleep(0.03)
            self.assertEqual(store.claim_due_goal()["id"], goal_id)

            autonomous = TurnDraft()
            updated = tools.execute(
                ToolCall(
                    "goal-update",
                    "goal_update",
                    {
                        "goal_id": goal_id,
                        "status": "waiting",
                        "waiting_for": "下一次检查时间",
                        "latest_result": "本次检查正常",
                        "next_review_at": (
                            datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
                        ).isoformat(),
                    },
                ),
                autonomous,
                authority="agent",
                source_event_id=f"goal:{goal_id}",
                allow_notify=True,
            )
            self.assertTrue(updated["ok"])
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "notify",
                        "send_bubbles",
                        {
                            "bubbles": ["检查完成", "目前正常", "没有数量上限", "继续观察"],
                            "reason": "任务阶段结果",
                            "key": "service.check",
                        },
                    ),
                    autonomous,
                    authority="agent",
                    source_event_id=f"goal:{goal_id}",
                    allow_notify=True,
                )["ok"]
            )
            self.assertEqual(len(autonomous.notification_messages or []), 4)
            store.commit_autonomous_turn(goal_id, autonomous)
            self.assertEqual(store.goal(goal_id)["status"], "waiting")
            first = store.due_outbox()[0]
            store.mark_sent(first.id)
            notification = store.claim_due_notification(NotificationConfig())
            self.assertIsNotNone(notification)
            self.assertTrue(store.queue_notification(str(notification["id"])))
            self.assertEqual(store.due_outbox()[0].text, "检查完成")
            notification_message = store._db.execute(
                """SELECT turn_id FROM messages
                   WHERE content='检查完成' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            self.assertEqual(notification_message["turn_id"], notification["turn_id"])
            store.mark_sent(store.due_outbox()[0].id)
            self.assertEqual(store.due_outbox()[0].text, "目前正常")
            episode = store.search_episodes("本次检查正常", 3)[0]
            archived = store.conversation_episode(str(episode["id"]))["messages"]
            self.assertTrue(
                any(
                    "AUTONOMOUS GOAL REVIEW RECORD" in item["content"]
                    for item in archived
                )
            )
            self.assertTrue(any("检查完成" in item["content"] for item in archived))
            store.close()

    def test_mood_update_persists_until_next_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:mood", "mood", "今天真开心", 1, 1)
            store.add_event(event)
            store.commit_turn(
                [event],
                event.text,
                AgentReply(
                    ["我也是！"],
                    mood_update={
                        "state": "excited",
                        "intensity": 0.8,
                        "cause": "一起分享了开心的事",
                    },
                ),
            )
            active = store.self_state()
            self.assertEqual(active["mood_state"], "excited")
            self.assertEqual(active["mood_intensity"], 0.8)
            self.assertEqual(active["mood_cause"], "一起分享了开心的事")
            store.close()

    def test_self_state_context_exposes_mood_update_time_and_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            with store._db:
                store._db.execute(
                    "UPDATE self_state SET mood_updated_at=? WHERE id=1", (1000,)
                )

            context = json.loads(store.self_state_context(now=1180))

            self.assertEqual(
                context["mood"]["updated_at"],
                context_timestamp(1000, store.timezone),
            )
            self.assertEqual(context["mood"]["age_minutes"], 3)
            store.close()

    def test_heartbeat_has_no_daily_evaluation_cap(self) -> None:
        now = datetime(2026, 7, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        heartbeat = HeartbeatConfig(
            enabled=True,
            initial_delay_seconds=60,
            min_interval_seconds=60,
            max_interval_seconds=600,
        )
        notifications = NotificationConfig()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("momoi.storage.store.time.time", return_value=now),
        ):
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            self.assertEqual(store.self_state()["next_heartbeat_at"], 0)
            store.ensure_heartbeat(heartbeat, now)
            self.assertEqual(store.next_heartbeat_due_at(True), now + 60)
            for index in range(20):
                turn_id = f"heartbeat-{index}"
                store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{index}"])
                store._db.execute(
                    "UPDATE self_state SET next_heartbeat_at=? WHERE id=1", (now - 1,)
                )
                store._db.commit()
                self.assertIsNotNone(
                    store.claim_due_heartbeat(heartbeat, notifications, now)
                )
                store.commit_heartbeat(
                    turn_id,
                    owner_event_revision=0,
                    notification_config=notifications,
                    activity="整理关卡灵感",
                    result="记录了一个点子",
                    next_heartbeat_at=now - 1,
                    mood_update=None,
                    messages=[],
                    reason="test",
                )
            episode = store.search_episodes("关卡灵感", 3)[0]
            self.assertEqual(len(store.episode_turns(str(episode["id"]))), 20)
            self.assertIn(
                "AUTONOMOUS HEARTBEAT RECORD",
                store.conversation_episode(str(episode["id"]))["messages"][-1][
                    "content"
                ],
            )
            store.close()

    def test_heartbeat_commits_memory_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage(
                "owner-evidence",
                "owner-evidence",
                "以后查微博登录状态时先看 session",
                time.time(),
                time.time(),
            )
            store.add_event(event)
            store.discard_events([event])
            draft = TurnDraft()
            memory = MemoryTools(store).execute(
                ToolCall(
                    "remember",
                    "memory_remember",
                    {
                        "kind": "shared",
                        "key": "shared.weibo.session_check",
                        "content": "查微博登录状态时先检查 session",
                        "evidence": event.text,
                        "activation": "recall",
                        "ttl_hours": 0,
                    },
                ),
                [event],
                draft,
            )
            self.assertTrue(memory["ok"])
            store.begin_turn("heartbeat-tools", "heartbeat", ["heartbeat:test"])
            store.commit_heartbeat(
                "heartbeat-tools",
                owner_event_revision=1,
                notification_config=NotificationConfig(),
                activity="整理微博使用方式",
                result="记下规则",
                next_heartbeat_at=time.time() + 3600,
                mood_update=None,
                messages=[],
                reason="test",
                draft=draft,
                memory_events=[event],
            )
            self.assertTrue(store.has_memory("shared", "shared.weibo.session_check"))
            store.close()

    def test_expected_reply_keeps_heartbeat_attention_until_owner_returns(self) -> None:
        heartbeat = HeartbeatConfig(
            enabled=False,
            initial_delay_seconds=60,
            min_interval_seconds=60,
            max_interval_seconds=600,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [],
                "",
                AgentReply(
                    ["今晚想吃什么？"],
                    reply_wait={
                        "wait": True,
                        "delay_minutes": 5,
                        "expected_information": "主人对晚餐的选择",
                        "reason": "晚餐需要按主人的选择来准备",
                    },
                ),
                turn_id="owner-question",
                target_channel="weixin",
            )
            row = store.due_outbox()[0]
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(row.id))
            initial_pending = store.pending_owner_reply(1000)
            self.assertEqual(
                initial_pending["expected_information"], "主人对晚餐的选择"
            )
            self.assertEqual(initial_pending["delay_minutes"], 5)
            self.assertEqual(
                initial_pending["reason"], "晚餐需要按主人的选择来准备"
            )
            self.assertEqual(store.next_heartbeat_due_at(False), 1300)
            self.assertIsNone(
                store.claim_episode_consolidation_candidate(minimum=1)
            )
            self.assertIsNotNone(
                store.claim_due_heartbeat(heartbeat, NotificationConfig(), now=1300)
            )

            store.begin_turn(
                "reply-followup", "reply_followup", ["reply-followup:1300"]
            )
            store.queue_progress(
                "reply-followup",
                "follow-up",
                ["还没想好的话，我可以帮你挑两个呀。"],
                "weixin",
            )
            with patch("momoi.storage.store.time.time", return_value=1300):
                store.commit_reply_followup(
                    "reply-followup",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    mood_update=None,
                    reason="晚餐需要按主人的选择来准备",
                    pending_reply_turn_id="owner-question",
                )
            self.assertIsNone(store.next_heartbeat_due_at(False))
            self.assertIsNotNone(store.pending_owner_reply(1300))
            key = store._db.execute(
                """SELECT notification_key FROM notifications
                   WHERE turn_id='reply-followup'"""
            ).fetchone()[0]
            self.assertEqual(key, "heartbeat.reply_followup")

            stale_followup = store.due_outbox()[0]
            self.assertEqual(stale_followup.channel, "weixin")
            self.assertEqual(
                store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?",
                    (stale_followup.id,),
                ).fetchone()[0],
                "",
            )
            with patch("momoi.storage.delivery.time.time", return_value=1310):
                self.assertFalse(store.mark_sent(stale_followup.id))
            self.assertIsNone(store.pending_owner_reply(1310))
            self.assertFalse(store.mark_sending(stale_followup.id))
            self.assertEqual(store.cooled_reply_expectation_context(1310), "")
            self.assertEqual(
                store._db.execute(
                    """SELECT COUNT(*) FROM messages
                       WHERE turn_id='reply-followup'"""
                ).fetchone()[0],
                0,
            )
            source_messages = [
                str(row["content"])
                for row in store._db.execute(
                    """SELECT content FROM messages
                       WHERE turn_id='owner-question' ORDER BY id"""
                ).fetchall()
            ]
            self.assertIn("还没想好的话，我可以帮你挑两个呀。", source_messages)
            candidate = store.claim_episode_consolidation_candidate(minimum=1)
            self.assertEqual(candidate["turns"][0]["turn_id"], "owner-question")
            self.assertTrue(
                any(
                    message["content"] == "还没想好的话，我可以帮你挑两个呀。"
                    for message in candidate["turns"][0]["messages"]
                )
            )
            store.close()

    def test_reply_followup_can_deliver_before_execution_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [],
                "",
                AgentReply(
                    ["老师会怎么选？"],
                    reply_wait={
                        "wait": True,
                        "delay_minutes": 1,
                        "expected_information": "老师的选择",
                        "reason": "需要老师决定下一步",
                    },
                ),
                turn_id="early-source",
            )
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                store.mark_sent(store.due_outbox()[0].id)
            store.begin_turn(
                "early-followup",
                "reply_followup",
                ["reply-followup:1060"],
            )
            store.queue_progress(
                "early-followup",
                "message",
                ["老师还没选呢"],
                "napcat",
            )
            followup = store.due_outbox()[0]
            with patch("momoi.storage.delivery.time.time", return_value=1060):
                store.mark_sent(followup.id)
            self.assertIsNotNone(store.pending_owner_reply(1060))

            with patch("momoi.storage.store.time.time", return_value=1061):
                store.commit_reply_followup(
                    "early-followup",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    pending_reply_turn_id="early-source",
                    reason="需要老师决定下一步",
                    mood_update=None,
                )
            self.assertIsNone(store.pending_owner_reply(1061))
            self.assertIn(
                "老师还没选呢",
                [
                    str(row["content"])
                    for row in store._db.execute(
                        "SELECT content FROM messages WHERE turn_id='early-source'"
                    ).fetchall()
                ],
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM messages WHERE turn_id='early-followup'"
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_wait_false_does_not_start_reply_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [],
                "",
                AgentReply(
                    ["喝完愿意的话跟我说一声"],
                    reply_wait={"wait": False},
                ),
                turn_id="passive-expectation",
            )
            row = store.due_outbox()[0]
            self.assertEqual(
                store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?", (row.id,)
                ).fetchone()[0],
                "",
            )
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertFalse(store.mark_sent(row.id))
            self.assertIsNone(store.pending_owner_reply(1000))
            self.assertIsNone(store.next_heartbeat_due_at(False))
            store.close()

    def test_expected_reply_can_follow_an_already_sent_progress_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("live-reply", "owner", ["owner-event"])
            store.queue_progress(
                "live-reply", "live-beat", ["还想听老师说后续"], "weixin"
            )
            outbox = store.due_outbox()[0]
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertFalse(store.mark_sent(outbox.id))

            with patch("momoi.storage.delivery.time.time", return_value=1010):
                store.commit_turn(
                    [],
                    "",
                    AgentReply(
                        [],
                        reply_wait={
                            "wait": True,
                            "delay_minutes": 3,
                            "expected_information": "老师想说的后续",
                            "reason": "想继续听老师说后续",
                        },
                    ),
                    turn_id="live-reply",
                )

            stored_wait = json.loads(
                store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?", (outbox.id,)
                ).fetchone()[0]
            )
            self.assertEqual(
                stored_wait["expected_information"], "老师想说的后续"
            )
            pending = store.pending_owner_reply(1010)
            self.assertEqual(pending["expected_information"], "老师想说的后续")
            self.assertEqual(store.next_heartbeat_due_at(False), 1190)
            store.close()

    def test_expected_reply_follows_pending_progress_only_after_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("queued-live-reply", "owner", ["owner-event"])
            store.queue_progress(
                "queued-live-reply", "live-beat", ["你会选哪一个？"], "napcat"
            )
            store.commit_turn(
                [],
                "",
                AgentReply(
                    [],
                    reply_wait={
                        "wait": True,
                        "delay_minutes": 3,
                        "expected_information": "老师的选择",
                        "reason": "需要按老师的选择继续",
                    },
                ),
                turn_id="queued-live-reply",
            )
            self.assertIsNone(store.pending_owner_reply())

            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(store.due_outbox()[0].id))
            self.assertEqual(store.next_heartbeat_due_at(False), 1180)
            store.close()

    def test_expected_reply_requires_a_visible_turn_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            with self.assertRaisesRegex(ValueError, "visible message"):
                store.commit_turn(
                    [],
                    "",
                    AgentReply(
                        [],
                        reply_wait={
                            "wait": True,
                            "delay_minutes": 3,
                            "expected_information": "不存在的消息",
                            "reason": "用于验证静默Turn不能创建等待",
                        },
                    ),
                    turn_id="silent-reply",
                )
            store.close()

    def test_owner_reply_cancels_only_reply_check_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [],
                "",
                AgentReply(
                    ["你会喜欢这种风险设计吗？"],
                    reply_wait={
                        "wait": True,
                        "delay_minutes": 4,
                        "expected_information": "主人的风险偏好",
                        "reason": "需要按老师的偏好继续设计风险机制",
                    },
                ),
                turn_id="question",
            )
            store._db.execute("UPDATE self_state SET next_heartbeat_at=4900 WHERE id=1")
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(store.due_outbox()[0].id))
            state = store.self_state()
            self.assertEqual(state["next_heartbeat_at"], 4900)
            self.assertEqual(state["pending_reply_next_check_at"], 1240)

            store.add_event(
                IncomingMessage("answer", "answer", "我喜欢求稳", 1020, 1020)
            )
            state = store.self_state()
            self.assertIsNone(state["pending_reply_next_check_at"])
            self.assertEqual(store.next_heartbeat_due_at(True), 4900)
            interrupted = json.loads(store.cooled_reply_expectation_context(1020))
            self.assertEqual(
                interrupted["state"], "owner_replied_before_deadline"
            )
            self.assertEqual(
                interrupted["expected_information"], "主人的风险偏好"
            )
            self.assertIn("风险机制", interrupted["reason"])
            store.close()

    def test_owner_reply_supersedes_claimed_undelivered_followup(self) -> None:
        heartbeat = HeartbeatConfig(
            enabled=False,
            initial_delay_seconds=60,
            min_interval_seconds=60,
            max_interval_seconds=600,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [],
                "",
                AgentReply(
                    ["还想听老师回答"],
                    reply_wait={
                        "wait": True,
                        "delay_minutes": 1,
                        "expected_information": "老师的回答",
                        "reason": "这个问题需要老师决定",
                    },
                ),
                turn_id="source-question",
            )
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                store.mark_sent(store.due_outbox()[0].id)
            self.assertIsNotNone(
                store.claim_due_heartbeat(
                    heartbeat, NotificationConfig(), now=1060
                )
            )
            store.begin_turn(
                "claimed-followup",
                "reply_followup",
                ["reply-followup:1060"],
            )
            store.queue_progress(
                "claimed-followup",
                "followup-message",
                ["老师还在吗"],
                "napcat",
            )
            stale = store.due_outbox()[0]

            store.add_event(
                IncomingMessage("owner-answer", "answer", "我在", 1061, 1061)
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT state FROM outbox WHERE id=?", (stale.id,)
                ).fetchone()[0],
                "superseded",
            )
            with patch("momoi.storage.store.time.time", return_value=1061):
                store.commit_reply_followup(
                    "claimed-followup",
                    owner_event_revision=1,
                    notification_config=NotificationConfig(),
                    pending_reply_turn_id="source-question",
                    reason="这个问题需要老师决定",
                    mood_update=None,
                )
            self.assertNotIn(
                "老师还在吗",
                [
                    str(row["content"])
                    for row in store._db.execute(
                        "SELECT content FROM messages WHERE turn_id='source-question'"
                    ).fetchall()
                ],
            )
            store.close()

    def test_reply_followup_clears_wait_without_changing_heartbeat_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """UPDATE self_state SET pending_reply_turn_id='question',
                   pending_reply_expectation='主人是否愿意继续聊',
                   pending_reply_since=1000,
                   pending_reply_last_reason='想知道老师是否继续聊',
                   pending_reply_delay_minutes=2,
                   pending_reply_next_check_at=1100,
                   next_heartbeat_at=1100 WHERE id=1"""
            )
            before = store.self_state()
            store.begin_turn(
                "reply-followup", "reply_followup", ["reply-followup:1100"]
            )
            store.queue_progress(
                "reply-followup",
                "message",
                ["还想听老师说说"],
                "napcat",
            )
            with patch("momoi.storage.store.time.time", return_value=1100):
                store.commit_reply_followup(
                    "reply-followup",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    mood_update=None,
                    reason="想知道老师是否继续聊",
                    pending_reply_turn_id="question",
                )
            self.assertIsNotNone(store.pending_owner_reply(1100))
            store.mark_sent(store.due_outbox()[0].id)
            self.assertIsNone(store.pending_owner_reply(1100))
            self.assertEqual(store.cooled_reply_expectation_context(1100), "")
            after = store.self_state()
            for key in (
                "activity",
                "activity_result",
                "last_heartbeat_at",
                "next_heartbeat_at",
            ):
                self.assertEqual(after[key], before[key])
            store.close()

    def test_owner_turn_consumes_interrupted_reply_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """UPDATE self_state SET cooled_reply_expectation='旧期待',
                   cooled_reply_source_turn_id='old', cooled_reply_since=1000,
                   cooled_reply_waiting_since=900, cooled_reply_due_at=1200,
                   cooled_reply_delay_minutes=5,
                   cooled_reply_reason='想听老师回答旧期待' WHERE id=1"""
            )
            interrupted = json.loads(store.cooled_reply_expectation_context(2000))
            self.assertEqual(interrupted["expected_information"], "旧期待")
            self.assertEqual(interrupted["reason"], "想听老师回答旧期待")
            with patch("momoi.storage.store.time.time", return_value=2000):
                store.commit_turn(
                    [],
                    "",
                    AgentReply([]),
                    turn_id="consume-expectation",
                )
            self.assertEqual(store.cooled_reply_expectation_context(2000), "")
            store.close()

    def test_reply_followup_never_schedules_a_second_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """UPDATE self_state SET pending_reply_turn_id='question',
                   pending_reply_expectation='主人是否愿意继续聊',
                   pending_reply_since=1000,
                   pending_reply_last_reason='想听老师回答',
                   pending_reply_delay_minutes=2,
                   pending_reply_next_check_at=1100 WHERE id=1"""
            )
            store.begin_turn(
                "single-followup", "reply_followup", ["reply-followup:1100"]
            )
            store.queue_progress(
                "single-followup",
                "message",
                ["老师还没回答呢"],
                "napcat",
            )
            with patch("momoi.storage.store.time.time", return_value=1100):
                store.commit_reply_followup(
                    "single-followup",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    mood_update=None,
                    reason="仍然想听主人回答",
                    pending_reply_turn_id="question",
                )
            self.assertIsNotNone(store.pending_owner_reply(1100))
            self.assertIsNone(store.next_heartbeat_due_at(False))
            store.mark_sent(store.due_outbox()[0].id)
            self.assertIsNone(store.pending_owner_reply(1100))
            self.assertEqual(store.cooled_reply_expectation_context(1100), "")
            store.close()

    def test_episode_anneal_waits_for_pending_owner_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("等待中的对话", episode_id="waiting-episode")
            for index in range(1, 4):
                turn_id = f"waiting-turn-{index}"
                store.commit_turn(
                    [],
                    f"owner-{index}",
                    AgentReply([f"assistant-{index}"]),
                    turn_id=turn_id,
                )
                store.mark_sent(store.due_outbox()[0].id)
                store.link_turn_to_episode("waiting-episode", turn_id)
            store._db.execute(
                """UPDATE self_state SET
                   pending_reply_turn_id='waiting-turn-3',
                   pending_reply_expectation='老师的回答',
                   pending_reply_last_reason='这段对话仍在等待老师',
                   pending_reply_delay_minutes=5,
                   pending_reply_next_check_at=1300 WHERE id=1"""
            )

            self.assertIsNone(store.claim_episode_annealing_candidate(1, 10000))

            store._db.execute(
                """UPDATE self_state SET pending_reply_turn_id=NULL,
                   pending_reply_expectation='', pending_reply_next_check_at=NULL
                   WHERE id=1"""
            )
            self.assertIsNotNone(
                store.claim_episode_annealing_candidate(1, 10000)
            )
            store.close()

    def test_new_owner_event_suppresses_heartbeat_visible_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            revision = int(
                store.heartbeat_conversation_snapshot()["owner_event_revision"]
            )
            store.begin_turn("stale-heartbeat", "heartbeat", ["heartbeat:1000"])
            store.add_event(
                IncomingMessage("new-owner", "new-owner", "嗯，我是 ISFJ", 1010, 1010)
            )
            committed = store.commit_heartbeat(
                "stale-heartbeat",
                owner_event_revision=revision,
                notification_config=NotificationConfig(),
                activity="继续想刚才的游戏机制",
                result="形成了一点看法",
                next_heartbeat_at=2000,
                mood_update=None,
                messages=["那就说得通了。", "你会舍不得开大，对不对？"],
                reason="继续刚才的话题",
            )

            self.assertEqual(committed, 0)
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
                0,
            )
            internal = store._db.execute(
                """SELECT delivery_state FROM messages
                   WHERE turn_id='stale-heartbeat'"""
            ).fetchall()
            self.assertEqual([row["delivery_state"] for row in internal], ["internal"])
            store.close()

    def test_heartbeat_live_progress_uses_same_turn_history_and_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("heartbeat-live", "heartbeat", ["heartbeat:1000"])
            store.queue_progress(
                "heartbeat-live", "live-beat", ["先跟老师说一声"], "napcat"
            )

            committed = store.commit_heartbeat(
                "heartbeat-live",
                owner_event_revision=0,
                notification_config=NotificationConfig(cooldown_seconds=0),
                activity="整理自己的关卡灵感",
                result="留下一个新点子",
                next_heartbeat_at=2000,
                mood_update=None,
                messages=["最后再补一句。"],
                reason="这次确实有值得分享的内容",
                notification_channel="napcat",
            )

            self.assertEqual(committed, 2)
            first = store.due_outbox()[0]
            self.assertEqual(first.text, "先跟老师说一声")
            store.mark_sent(first.id)
            self.assertEqual(
                [row.text for row in store.due_outbox()], ["最后再补一句。"]
            )
            visible = store._db.execute(
                """SELECT content FROM messages
                   WHERE turn_id='heartbeat-live' AND role='assistant'
                     AND delivery_state<>'internal'
                   ORDER BY id"""
            ).fetchall()
            self.assertEqual(
                [row["content"] for row in visible],
                ["先跟老师说一声", "最后再补一句。"],
            )
            store.close()

    def test_orphaned_owner_turn_does_not_block_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("orphaned-owner", "owner", ["missing-event"])
            store.record_turn_failure("orphaned-owner", "ProviderError")

            self.assertFalse(store.heartbeat_conversation_snapshot()["owner_busy"])

            store.add_event(
                IncomingMessage("current-owner", "current-owner", "还在吗", 1, 1)
            )
            snapshot = store.heartbeat_conversation_snapshot()
            self.assertTrue(snapshot["owner_busy"])
            self.assertEqual(snapshot["blocked_by"], "pending_owner_event")
            store.close()

    def test_heartbeat_cooldown_suppresses_instead_of_delaying_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, state, not_before, created_at, queued_at)
                   VALUES ('previous', 'previous-turn', 'heartbeat',
                           'heartbeat.chat', 'normal', 'previous', '["旧消息"]',
                           'queued', 1000, 1000, 1000)"""
            )
            store.begin_turn("cooldown-heartbeat", "heartbeat", ["heartbeat:1100"])
            with patch("momoi.storage.store.time.time", return_value=1100):
                committed = store.commit_heartbeat(
                    "cooldown-heartbeat",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(cooldown_seconds=1800),
                    activity="继续想游戏机制",
                    result="形成了一点看法",
                    next_heartbeat_at=2000,
                    mood_update=None,
                    messages=["这句现在不能发。"],
                    reason="继续话题",
                )

            self.assertEqual(committed, 0)
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
                1,
            )
            self.assertEqual(store.due_outbox(), [])
            store.close()

    def test_reply_followup_contact_bypasses_normal_notification_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, state, not_before, created_at, queued_at)
                   VALUES ('previous-reply', 'previous-reply-turn', 'heartbeat',
                           'heartbeat.reply_followup', 'normal', 'previous',
                           '[\"旧催促\"]', 'queued', 1000, 1000, 1000)"""
            )
            store._db.commit()
            config = NotificationConfig(cooldown_seconds=1800)

            ordinary = store.heartbeat_contact_window(
                "heartbeat.reply_followup", config, now=1100
            )
            reply_followup = store.heartbeat_contact_window(
                "heartbeat.reply_followup", config, now=1100, apply_cooldown=False
            )

            self.assertFalse(ordinary["allowed"])
            self.assertTrue(reply_followup["allowed"])
            self.assertEqual(reply_followup["eligible_at"], 1100)
            store.close()

    def test_owner_message_supersedes_queued_heartbeat_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("heartbeat-chat", "heartbeat", ["heartbeat:1000"])
            with patch("momoi.storage.store.time.time", return_value=1000):
                committed = store.commit_heartbeat(
                    "heartbeat-chat",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(cooldown_seconds=0),
                    activity="想起游戏机制",
                    result="形成了一点看法",
                    next_heartbeat_at=2000,
                    mood_update=None,
                    messages=["这是一句瞬时聊天。"],
                    reason="自然分享",
                )
            self.assertEqual(committed, 1)
            self.assertEqual(store.due_outbox()[0].text, "这是一句瞬时聊天。")

            store.add_event(
                IncomingMessage(
                    "owner-moved-on", "owner-moved-on", "换个话题", 1010, 1010
                )
            )

            notification = store._db.execute(
                "SELECT state, superseded_reason FROM notifications"
            ).fetchone()
            self.assertEqual(notification["state"], "superseded")
            self.assertEqual(
                notification["superseded_reason"],
                "owner_message_superseded_heartbeat_contact",
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT state FROM outbox WHERE turn_id='heartbeat-chat'"
                ).fetchone()[0],
                "superseded",
            )
            self.assertEqual(store.due_outbox(), [])
            visible = store._db.execute(
                """SELECT COUNT(*) FROM messages WHERE turn_id='heartbeat-chat'
                   AND delivery_state<>'internal'"""
            ).fetchone()[0]
            self.assertEqual(visible, 0)
            store.close()

    def test_reply_attention_never_delays_an_earlier_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [],
                "",
                AgentReply(
                    ["到家了吗？"],
                    reply_wait={
                        "wait": True,
                        "delay_minutes": 3,
                        "expected_information": "主人是否已经到家",
                        "reason": "想确认老师已经安全到家",
                    },
                ),
                turn_id="question",
            )
            store._db.execute("UPDATE self_state SET next_heartbeat_at=1030 WHERE id=1")
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(store.due_outbox()[0].id))
            self.assertEqual(store.next_heartbeat_due_at(False), 1180)
            self.assertEqual(store.next_heartbeat_due_at(True), 1030)
            store.close()

    def test_recurring_goal_persists_schedule_and_advances_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage(
                "qq:1:recurring", "recurring", "每小时检查一次服务", 1, 1
            )
            store.add_event(event)
            draft = TurnDraft()
            created = tools.execute(
                ToolCall(
                    "goal-recurring",
                    "goal_create",
                    {
                        "title": "检查服务",
                        "success_criteria": "每次确认服务状态",
                        "next_action": "检查服务状态",
                        "schedule": {
                            "kind": "interval",
                            "every_seconds": 3600,
                        },
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )
            self.assertTrue(created["ok"], created)
            goal_id = created["goal"]["id"]
            store.commit_turn([event], event.text, AgentReply(["会定期检查"]), draft)
            self.assertEqual(store.goal(goal_id)["schedule"]["kind"], "interval")

            occurrence_draft = TurnDraft()
            occurrence = tools.execute(
                ToolCall(
                    "goal-update-occurrence",
                    "goal_update",
                    {
                        "goal_id": goal_id,
                        "status": "active",
                        "latest_result": "本次检查正常",
                    },
                ),
                occurrence_draft,
                authority="agent",
                source_event_id="goal-review",
                allow_notify=True,
            )
            self.assertTrue(occurrence["ok"], occurrence)
            self.assertEqual(occurrence["goal"]["status"], "active")
            self.assertIsNotNone(occurrence["goal"]["next_review_at"])

            finished = tools.execute(
                ToolCall(
                    "goal-finish-overall",
                    "goal_finish",
                    {"goal_id": goal_id, "result": "总体成功标准已达成"},
                ),
                TurnDraft(),
                authority="agent",
                source_event_id="goal-review",
                allow_notify=True,
            )
            self.assertTrue(finished["ok"], finished)
            self.assertEqual(finished["goal"]["status"], "done")

            cancelled = tools.execute(
                ToolCall(
                    "goal-cancel-overall",
                    "goal_cancel",
                    {"goal_id": goal_id, "reason": "周期任务已明确停止"},
                ),
                TurnDraft(),
                authority="owner",
                source_event_id="owner-stop",
                allow_notify=False,
            )
            self.assertTrue(cancelled["ok"], cancelled)
            self.assertEqual(cancelled["goal"]["status"], "cancelled")

            store._db.execute(
                "UPDATE goals SET next_review_at=? WHERE id=?",
                (time.time() - 1, goal_id),
            )
            store._db.commit()
            self.assertEqual(store.claim_due_goal()["id"], goal_id)
            before = time.time()
            store.commit_autonomous_turn(goal_id, TurnDraft())
            advanced = store.goal(goal_id)
            self.assertEqual(advanced["status"], "active")
            self.assertGreaterEqual(advanced["next_review_at"], before + 3599)
            self.assertIsNone(advanced["review_claimed_at"])

            after = datetime(2026, 7, 20, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
            next_daily = next_schedule_at(
                {
                    "kind": "daily",
                    "times": ["08:00"],
                },
                ZoneInfo("Asia/Shanghai"),
                after.timestamp(),
            )
            self.assertEqual(
                datetime.fromtimestamp(next_daily, ZoneInfo("Asia/Shanghai")),
                datetime(2026, 7, 21, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            with self.assertRaisesRegex(ValueError, "requires 1 to 24 times"):
                normalize_schedule(
                    {
                        "kind": "daily",
                        "at": "08:00",
                    }
                )

            next_same_day = next_schedule_at(
                {
                    "kind": "daily",
                    "times": ["20:00", "08:00", "12:00"],
                },
                ZoneInfo("Asia/Shanghai"),
                after.timestamp(),
            )
            self.assertEqual(
                datetime.fromtimestamp(next_same_day, ZoneInfo("Asia/Shanghai")),
                datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            next_day = next_schedule_at(
                {
                    "kind": "daily",
                    "times": ["08:00", "12:00", "20:00"],
                },
                ZoneInfo("Asia/Shanghai"),
                datetime(2026, 7, 20, 20, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(),
            )
            self.assertEqual(
                datetime.fromtimestamp(next_day, ZoneInfo("Asia/Shanghai")),
                datetime(2026, 7, 21, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            store.close()

    def test_legacy_goal_timezone_is_removed_without_delaying_due_work(self) -> None:
        zone = ZoneInfo("Asia/Shanghai")
        startup = datetime(2030, 1, 1, 7, 0, tzinfo=zone).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            draft = TurnDraft()
            created = AgendaTools(store).execute(
                ToolCall(
                    "legacy-daily",
                    "goal_create",
                    {
                        "title": "晨间检查",
                        "success_criteria": "每天完成检查",
                        "next_action": "检查",
                        "schedule": {"kind": "daily", "times": ["08:00"]},
                    },
                ),
                draft,
                authority="owner",
                source_event_id="test",
                allow_notify=False,
            )
            self.assertTrue(created["ok"], created)
            store.commit_goal_draft(draft)
            goal_id = str(created["goal"]["id"])
            with store._db:
                store._db.execute(
                    "UPDATE goals SET schedule_json=?, next_review_at=? WHERE id=?",
                    (
                        json.dumps(
                            {
                                "kind": "daily",
                                "timezone": "UTC",
                                "times": ["08:00"],
                            }
                        ),
                        datetime(2030, 1, 2, tzinfo=zone).timestamp(),
                        goal_id,
                    ),
                )
            store.close()

            with patch("momoi.storage.store.time.time", return_value=startup):
                store = Store(path, timezone="Asia/Shanghai")
            goal = store.goal(goal_id)
            self.assertNotIn("timezone", goal["schedule"])
            self.assertEqual(
                datetime.fromtimestamp(goal["next_review_at"], zone),
                datetime(2030, 1, 1, 8, 0, tzinfo=zone),
            )
            with store._db:
                store._db.execute(
                    "UPDATE goals SET next_review_at=? WHERE id=?",
                    (startup - 1, goal_id),
                )
            store.close()

            with patch("momoi.storage.store.time.time", return_value=startup):
                store = Store(path, timezone="Asia/Shanghai")
            self.assertEqual(store.goal(goal_id)["next_review_at"], startup - 1)
            store.close()

    def test_notification_policy_delays_without_replaying_goal_work(self) -> None:
        def add(
            store: Store,
            notification_id: str,
            key: str,
            priority: str,
            created_at: float,
        ) -> None:
            store._db.execute(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, state, not_before, created_at)
                   VALUES (?, ?, 'goal', ?, ?, 'test', '[\"状态更新\"]',
                           'pending', ?, ?)""",
                (
                    notification_id,
                    f"turn:{notification_id}",
                    key,
                    priority,
                    created_at,
                    created_at,
                ),
            )
            store._db.commit()

        zone = ZoneInfo("Asia/Shanghai")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            quiet = NotificationConfig(
                quiet_start="23:00",
                quiet_end="08:00",
                cooldown_seconds=3600,
            )
            late = datetime(2030, 1, 1, 23, 30, tzinfo=zone).timestamp()
            add(store, "quiet", "service.status", "normal", late)
            self.assertIsNone(store.claim_due_notification(quiet, late))
            morning = datetime(2030, 1, 2, 8, 0, tzinfo=zone).timestamp()
            claimed = store.claim_due_notification(quiet, morning)
            self.assertEqual(claimed["id"], "quiet")
            self.assertTrue(store.queue_notification("quiet", morning, quiet))

            add(store, "cooldown", "service.status", "normal", morning + 60)
            self.assertIsNone(store.claim_due_notification(quiet, morning + 60))
            not_before = store._db.execute(
                "SELECT not_before FROM notifications WHERE id='cooldown'"
            ).fetchone()[0]
            self.assertGreaterEqual(not_before, morning + 3600)

            store.close()

        with tempfile.TemporaryDirectory() as directory:
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            now = datetime(2030, 1, 2, 10, 0, tzinfo=zone).timestamp()
            pending = IncomingMessage("qq:pending", "pending", "主人消息", now, now)
            store.add_event(pending)
            add(store, "normal", "normal.status", "normal", now)
            add(store, "urgent", "urgent.failure", "urgent", now)
            policy = NotificationConfig(
                cooldown_seconds=0,
                pending_owner_delay_seconds=60,
            )
            self.assertIsNone(store.claim_due_notification(policy, now))
            claimed = store.claim_due_notification(policy, now)
            self.assertEqual(claimed["id"], "urgent")
            self.assertTrue(store.queue_notification("urgent", now, policy))
            normal_due = store._db.execute(
                "SELECT not_before FROM notifications WHERE id='normal'"
            ).fetchone()[0]
            self.assertEqual(normal_due, now + 60)
            store.close()

    def test_notification_policy_has_no_daily_delivery_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(2030, 1, 2, 10, 0).timestamp()
            store._db.executemany(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, state, not_before, created_at, queued_at)
                   VALUES (?, ?, 'goal', ?, 'normal', 'test', '["状态更新"]',
                           'queued', ?, ?, ?)""",
                [
                    (f"sent-{index}", f"turn-{index}", f"key-{index}", now, now, now)
                    for index in range(12)
                ],
            )
            store._db.execute(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, state, not_before, created_at)
                   VALUES ('next', 'turn-next', 'heartbeat', 'heartbeat.chat',
                           'normal', 'test', '["还在吗？"]', 'pending', ?, ?)""",
                (now, now),
            )
            claimed = store.claim_due_notification(
                NotificationConfig(cooldown_seconds=0), now
            )
            self.assertEqual(claimed["id"], "next")
            store.close()

    def test_event_turn_and_outbox_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:2", "2", "你好", 1.0, 2.0)
            self.assertTrue(store.add_event(event))
            self.assertFalse(store.add_event(event))
            self.assertEqual(len(store.pending_events()), 1)
            store.commit_turn(
                [event],
                "你好",
                AgentReply(["嘿嘿，没忘吧~", "晚上在忙什么呢？"]),
            )
            self.assertEqual(store.pending_events(), [])
            outbox = store.due_outbox()
            self.assertEqual(len(outbox), 1)
            self.assertEqual(outbox[0].text, "嘿嘿，没忘吧~")
            store.mark_sent(outbox[0].id)
            self.assertEqual(store.due_outbox()[0].text, "晚上在忙什么呢？")
            store.close()

    def test_structured_channel_messages_are_persisted_in_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:rich-out", "rich-out", "发给我", 1, 1)
            store.add_event(event)
            store.commit_turn(
                [event],
                "发给我",
                AgentReply(
                    [
                        {
                            "segments": [
                                {"type": "reply", "data": {"id": "42"}},
                                {"type": "text", "data": {"text": "收到"}},
                                {
                                    "type": "image",
                                    "data": {"file": "https://img.example/a.jpg"},
                                },
                            ]
                        }
                    ]
                ),
            )
            row = store.due_outbox()[0]
            self.assertEqual(row.kind, "message")
            self.assertEqual(row.payload["action"], "message")
            self.assertEqual(
                [part["type"] for part in row.payload["segments"]],
                ["reply", "text", "image"],
            )
            history = store._db.execute(
                "SELECT content FROM messages WHERE role='assistant'"
            ).fetchone()[0]
            self.assertIn("reply to message_id=42", history)
            self.assertIn("https://img.example/a.jpg", history)
            store.close()

    def test_progress_message_is_queued_before_final_reply_and_committed_to_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage("qq:1:progress", "progress", "执行任务", 1, 1)
            store.add_event(event)
            store.begin_turn("turn-progress", "owner", [event.event_id])
            store.queue_progress("turn-progress", "call-progress", ["正在处理"])
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["处理完成"]),
                turn_id="turn-progress",
            )

            self.assertEqual(
                [
                    row["content"]
                    for row in store._db.execute(
                        "SELECT content FROM messages ORDER BY id"
                    ).fetchall()
                ],
                ["执行任务", "正在处理", "处理完成"],
            )
            rows = store._db.execute(
                "SELECT role, content, turn_id FROM messages ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [(row["role"], row["content"]) for row in rows],
                [
                    ("user", "执行任务"),
                    ("assistant", "正在处理"),
                    ("assistant", "处理完成"),
                ],
            )
            self.assertEqual({row["turn_id"] for row in rows}, {"turn-progress"})
            first = store.due_outbox()[0]
            self.assertEqual(first.text, "正在处理")
            store.mark_sent(first.id)
            self.assertEqual(store.due_outbox()[0].text, "处理完成")
            store.close()

    def test_memory_survives_history_window_and_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            event = IncomingMessage(
                "qq:1:memory-1",
                "memory-1",
                "以后卧室灯都用暖色，记住哦",
                1.0,
                1.0,
            )
            store.add_event(event)
            draft = TurnDraft()
            result = MemoryTools(store).execute(
                ToolCall(
                    "tool-1",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.bedroom.light_color",
                        "content": "卧室灯默认使用暖色",
                        "evidence": "卧室灯都用暖色",
                        "importance": 0.9,
                    },
                ),
                [event],
                draft,
            )
            self.assertTrue(result["ok"])
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["记住啦"]),
                draft,
            )
            self.assertIn(
                "卧室灯默认使用暖色",
                str(store.search_memories("卧室灯", 6)),
            )

            correction = IncomingMessage(
                "qq:1:memory-2",
                "memory-2",
                "改一下，以后卧室灯用冷色",
                2.0,
                2.0,
            )
            store.add_event(correction)
            correction_draft = TurnDraft()
            result = MemoryTools(store).execute(
                ToolCall(
                    "tool-2",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.bedroom.light_color",
                        "content": "卧室灯默认使用冷色",
                        "evidence": "卧室灯用冷色",
                        "importance": 0.9,
                        "replace_confirmed": True,
                    },
                ),
                [correction],
                correction_draft,
            )
            self.assertTrue(result["ok"])
            store.commit_turn(
                [correction],
                correction.text,
                AgentReply(["改成冷色了"]),
                correction_draft,
            )
            recalled = str(store.search_memories("卧室灯", 6))
            self.assertIn("卧室灯默认使用冷色", recalled)
            self.assertNotIn("卧室灯默认使用暖色", recalled)

            for index in range(30):
                item = IncomingMessage(
                    f"qq:1:{index + 10}",
                    str(index + 10),
                    f"第{index}轮",
                    float(index + 10),
                    float(index + 10),
                )
                store.add_event(item)
                store.commit_turn([item], item.text, AgentReply([f"回复{index}"]))
            self.assertGreater(
                store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                60,
            )
            store.close()
            store = Store(path)
            self.assertIn(
                "卧室灯默认使用冷色",
                str(store.search_memories("卧室灯", 6)),
            )
            store.close()

    def test_memory_overwrite_waits_for_owner_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            original = IncomingMessage(
                "qq:1:original", "original", "记住我喜欢暖色灯", 1, 1
            )
            store.add_event(original)
            draft = TurnDraft()
            tools.execute(
                ToolCall(
                    "remember-original",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.light.color",
                        "content": "主人喜欢暖色灯",
                        "evidence": "我喜欢暖色灯",
                    },
                ),
                [original],
                draft,
            )
            store.commit_turn([original], original.text, AgentReply(["记住了"]), draft)

            uncertain = IncomingMessage(
                "qq:1:uncertain", "uncertain", "也许我更喜欢冷色灯", 2, 2
            )
            store.add_event(uncertain)
            conflict_draft = TurnDraft()
            result = tools.execute(
                ToolCall(
                    "remember-uncertain",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.light.color",
                        "content": "主人喜欢冷色灯",
                        "evidence": "也许我更喜欢冷色灯",
                    },
                ),
                [uncertain],
                conflict_draft,
            )
            self.assertEqual(result["state"], "conflict_pending")
            store.commit_turn(
                [uncertain],
                uncertain.text,
                AgentReply(["你想改成冷色吗"]),
                conflict_draft,
            )
            self.assertEqual(
                store.active_memory("preference", "home.light.color")["content"],
                "主人喜欢暖色灯",
            )

            confirmed = IncomingMessage(
                "qq:1:confirmed", "confirmed", "对，改成冷色灯", 3, 3
            )
            store.add_event(confirmed)
            confirmed_draft = TurnDraft()
            result = tools.execute(
                ToolCall(
                    "remember-confirmed",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "home.light.color",
                        "content": "主人喜欢冷色灯",
                        "evidence": "改成冷色灯",
                        "replace_confirmed": True,
                    },
                ),
                [confirmed],
                confirmed_draft,
            )
            self.assertEqual(result["state"], "staged")
            store.commit_turn(
                [confirmed], confirmed.text, AgentReply(["已经改好了"]), confirmed_draft
            )
            self.assertEqual(
                store.active_memory("preference", "home.light.color")["content"],
                "主人喜欢冷色灯",
            )
            store.close()

    def test_repeated_identical_memory_updates_evidence_without_duplication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            for index, text in enumerate(
                ["记住，我喜欢暖色灯", "再说一次，我喜欢暖色灯"], start=1
            ):
                event = IncomingMessage(
                    f"qq:1:same-memory-{index}",
                    f"same-memory-{index}",
                    text,
                    float(index),
                    float(index),
                )
                store.add_event(event)
                draft = TurnDraft()
                result = tools.execute(
                    ToolCall(
                        f"remember-{index}",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "home.light.color",
                            "content": "主人偏好暖色灯光",
                            "evidence": "我喜欢暖色灯",
                            "importance": 0.7 + index / 10,
                        },
                    ),
                    [event],
                    draft,
                )
                self.assertTrue(result["ok"])
                store.commit_turn([event], text, AgentReply(["记住了"]), draft)

            rows = store._db.execute(
                "SELECT id, source_event_id, evidence_quote, importance FROM memories"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_event_id"], "qq:1:same-memory-2")
            self.assertEqual(rows[0]["evidence_quote"], "我喜欢暖色灯")
            self.assertAlmostEqual(rows[0]["importance"], 0.9)
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM memory_evidence WHERE memory_id=?",
                    (rows[0]["id"],),
                ).fetchone()[0],
                2,
            )
            store.close()

    def test_memory_forget_requires_owner_evidence_and_can_be_relearned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            remembered = IncomingMessage(
                "qq:1:remember-forget", "remember-forget", "记住我喜欢暖色", 1, 1
            )
            store.add_event(remembered)
            draft = TurnDraft()
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "remember",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "content": "主人喜欢暖色",
                            "evidence": "我喜欢暖色",
                        },
                    ),
                    [remembered],
                    draft,
                )["ok"]
            )
            store.commit_turn(
                [remembered], remembered.text, AgentReply(["记住了"]), draft
            )

            uncertain = IncomingMessage(
                "qq:1:forget-conflict", "forget-conflict", "也许我喜欢冷色", 1.5, 1.5
            )
            store.add_event(uncertain)
            conflict_draft = TurnDraft()
            self.assertEqual(
                tools.execute(
                    ToolCall(
                        "remember-conflict",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "content": "主人喜欢冷色",
                            "evidence": "也许我喜欢冷色",
                        },
                    ),
                    [uncertain],
                    conflict_draft,
                )["state"],
                "conflict_pending",
            )
            store.commit_turn(
                [uncertain], uncertain.text, AgentReply(["需要你确认"]), conflict_draft
            )

            forgotten = IncomingMessage(
                "qq:1:forget", "forget", "忘掉灯光颜色偏好", 2, 2
            )
            store.add_event(forgotten)
            invalid = tools.execute(
                ToolCall(
                    "forget-invalid",
                    "memory_forget",
                    {
                        "kind": "preference",
                        "key": "light.color",
                        "evidence": "这段话并不存在",
                    },
                ),
                [forgotten],
                TurnDraft(),
            )
            self.assertEqual(invalid["error"], "evidence_not_in_current_input")

            forget_draft = TurnDraft()
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "forget",
                        "memory_forget",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "evidence": "忘掉灯光颜色偏好",
                        },
                    ),
                    [forgotten],
                    forget_draft,
                )["ok"]
            )
            store.commit_turn(
                [forgotten], forgotten.text, AgentReply(["已经忘掉了"]), forget_draft
            )
            self.assertEqual(store.search_memories("冷色", 6), [])
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1
            )

            relearned = IncomingMessage(
                "qq:1:relearn", "relearn", "重新记住我喜欢冷色", 3, 3
            )
            store.add_event(relearned)
            relearn_draft = TurnDraft()
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "relearn",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": "light.color",
                            "content": "主人喜欢冷色",
                            "evidence": "我喜欢冷色",
                        },
                    ),
                    [relearned],
                    relearn_draft,
                )["ok"]
            )
            store.commit_turn(
                [relearned], relearned.text, AgentReply(["重新记住了"]), relearn_draft
            )
            self.assertIn("主人喜欢冷色", str(store.search_memories("冷色", 6)))
            store.close()

    def test_memory_remember_validates_boundary_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            evidence = "证" * 500
            event = IncomingMessage("qq:1:bounds", "bounds", evidence, 1, 1)
            valid = {
                "kind": "preference",
                "key": "k" * 200,
                "content": "内" * 2000,
                "evidence": evidence,
                "importance": 2,
            }
            draft = TurnDraft()
            accepted = tools.execute(
                ToolCall("valid", "memory_remember", valid), [event], draft
            )
            self.assertTrue(accepted["ok"])
            self.assertEqual(draft.memories[0].importance, 1.0)

            invalid = [
                ({**valid, "kind": "unknown"}, "invalid_kind"),
                ({**valid, "key": "UPPER"}, "invalid_key"),
                ({**valid, "key": "k" * 201}, "invalid_key"),
                ({**valid, "content": ""}, "invalid_content"),
                ({**valid, "content": "内" * 2001}, "invalid_content"),
                (
                    {**valid, "evidence": "不在当前消息里"},
                    "evidence_not_in_current_input",
                ),
                ({**valid, "evidence": "证" * 501}, "evidence_not_in_current_input"),
                ({**valid, "replace_confirmed": "yes"}, "invalid_replace_confirmed"),
                (
                    {**valid, "activation": "recent", "ttl_hours": 0},
                    "invalid_ttl",
                ),
                (
                    {**valid, "activation": "recent", "ttl_hours": 721},
                    "invalid_ttl",
                ),
            ]
            for index, (arguments, error) in enumerate(invalid):
                result = tools.execute(
                    ToolCall(f"invalid-{index}", "memory_remember", arguments),
                    [event],
                    TurnDraft(),
                )
                self.assertEqual(result["error"], error)
                self.assertTrue(result["message"])
            store.close()

    def test_memory_activation_layers_are_compact_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            cases = [
                (
                    "always",
                    "communication.punctuation.tilde",
                    "老师不喜欢日常聊天使用波浪号",
                    "不要用波浪号",
                ),
                (
                    "recent",
                    "current.recovery",
                    "老师这几天在恢复身体状态",
                    "这几天在恢复身体状态",
                ),
                (
                    "recall",
                    "hobby.cycling",
                    "老师喜欢骑车",
                    "喜欢骑车",
                ),
            ]
            for index, (activation, key, content, evidence) in enumerate(cases):
                event = IncomingMessage(
                    f"activation-{index}", f"activation-{index}", evidence, 1, 1
                )
                store.add_event(event)
                draft = TurnDraft()
                result = tools.execute(
                    ToolCall(
                        f"remember-{index}",
                        "memory_remember",
                        {
                            "kind": "preference",
                            "key": key,
                            "content": content,
                            "evidence": evidence,
                            "activation": activation,
                            "ttl_hours": 48 if activation == "recent" else 0,
                        },
                    ),
                    [event],
                    draft,
                )
                self.assertTrue(result["ok"])
                store.commit_turn([event], event.text, AgentReply(["记住了"]), draft)

            always = store.always_memory_context()
            recent = store.recent_memory_context()
            recalled = str(store.search_memories("骑车", 6))
            self.assertIn("波浪号", always)
            self.assertNotIn("骑车", always)
            self.assertIn("恢复身体", recent)
            self.assertNotIn("波浪号", recent)
            self.assertIn("喜欢骑车", recalled)
            self.assertNotIn("波浪号", recalled)
            self.assertNotIn("恢复身体", recalled)
            self.assertEqual(always.count("波浪号"), 1)
            recent_row = store._db.execute(
                "SELECT expires_at FROM memories WHERE key='current.recovery'"
            ).fetchone()
            self.assertIsNotNone(recent_row["expires_at"])
            always_row = store._db.execute(
                "SELECT expires_at FROM memories WHERE key='communication.punctuation.tilde'"
            ).fetchone()
            self.assertIsNone(always_row["expires_at"])
            store.close()

    def test_recent_memory_drops_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = MemoryTools(store)
            event = IncomingMessage(
                "qq:ttl", "ttl", "现在窝在沙发上", 1, 1
            )
            store.add_event(event)
            draft = TurnDraft()
            result = tools.execute(
                ToolCall(
                    "remember-ttl",
                    "memory_remember",
                    {
                        "kind": "episodic",
                        "key": "current.sofa",
                        "content": "小桃现在抱着靠枕窝在沙发上。",
                        "evidence": "窝在沙发上",
                        "activation": "recent",
                        "ttl_hours": 2,
                    },
                ),
                [event],
                draft,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["memory"]["ttl_hours"], 2)
            before = time.time()
            store.commit_turn([event], event.text, AgentReply(["记住了"]), draft)
            expires_at = store._db.execute(
                "SELECT expires_at FROM memories WHERE key='current.sofa'"
            ).fetchone()["expires_at"]
            self.assertGreaterEqual(expires_at, before + 2 * 3600 - 1)
            self.assertLessEqual(expires_at, time.time() + 2 * 3600 + 1)
            self.assertIn("沙发", store.recent_memory_context())
            store._db.execute(
                "UPDATE memories SET expires_at=? WHERE key='current.sofa'",
                (time.time() - 1,),
            )
            store._db.commit()
            self.assertNotIn("沙发", store.recent_memory_context())
            self.assertEqual(store.list_memories(), [])
            store.close()
