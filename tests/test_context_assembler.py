import tempfile
import time
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage
from momoi.runtime.context_assembler import (
    _search_or,
    assemble_main_context,
    build_plan_retrieval,
    recall_episode_context,
)
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
        "episode_bindings": [
            {
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
            empty_plan["intent_units"][0]["recall_queries"] = []
            retrieval = build_plan_retrieval(
                store,
                empty_plan,
                config(directory, recent_episode_hours=6),
            )

            self.assertEqual(
                [item["episode_id"] for item in retrieval["episodes"]],
                [f"recent-topic-{index:02d}" for index in range(13)],
            )
            self.assertTrue(
                all(item["relation"] == "recent" for item in retrieval["episodes"])
            )
            self.assertTrue(
                all(item["unit_ids"] == [] for item in retrieval["episodes"])
            )
            assembled = assemble_main_context(store, retrieval, 2000, 2000)
            self.assertIn("最近六小时话题 12", assembled["episodes"])
            self.assertNotIn("六小时前旧话题", assembled["episodes"])

            disabled = build_plan_retrieval(
                store,
                empty_plan,
                config(directory, recent_episode_hours=0),
            )
            self.assertEqual(disabled["episodes"], [])
            store.close()

    def test_recent_and_keyword_episode_is_injected_once(self) -> None:
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
                retrieval["episodes"][0]["relation"], "recent_recalled"
            )
            self.assertEqual(retrieval["episodes"][0]["unit_ids"], ["mail"])
            store.close()

    def test_keyword_recall_is_capped_at_twelve_before_recent_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            for index in range(13):
                episode_id = f"keyword-{index:02d}"
                turn_id = f"keyword-turn-{index:02d}"
                store.create_episode(
                    f"关键词话题 {index:02d}", episode_id=episode_id
                )
                store.begin_turn(turn_id, "autonomous", [turn_id])
                timestamp = now - 7 * 3600 - index * 60
                with store._db:
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, '[]', 'internal')""",
                        (turn_id, f"关键词内容 {index:02d}", timestamp),
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

            self.assertEqual(len(retrieval["episodes"]), 12)
            self.assertNotIn(
                "keyword-12",
                {item["episode_id"] for item in retrieval["episodes"]},
            )
            self.assertTrue(
                all(item["relation"] == "recalled" for item in retrieval["episodes"])
            )
            store.close()

    def test_episode_merge_order_prefers_recent_multi_then_keyword_hits_then_recent(
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

            self.assertEqual(
                [item["episode_id"] for item in retrieval["episodes"]],
                [
                    "recent-multi",
                    "old-multi",
                    "recent-single",
                    "old-single",
                    "recent-only",
                ],
            )
            self.assertEqual(
                [item["keyword_match_count"] for item in retrieval["episodes"]],
                [2, 2, 1, 1, 0],
            )
            store.close()

    def test_planner_or_query_is_executed_as_separate_terms(self) -> None:
        calls: list[str] = []
        rows = {
            "房间": [{"id": "shared"}, {"id": "room-only"}],
            "屋子": [{"id": "shared"}, {"id": "house-only"}],
            "碎片": [{"id": "fragment-only"}],
        }

        def search(query: str, _: int) -> list[dict[str, object]]:
            calls.append(query)
            return rows[query]

        results = _search_or(
            "房间 | 屋子 | 碎片 | 房间",
            search,
            lambda row: row["id"],
            4,
        )

        self.assertEqual(calls, ["房间", "屋子", "碎片"])
        self.assertEqual(
            [row["id"] for row in results],
            ["shared", "room-only", "house-only", "fragment-only"],
        )

    def test_expanded_query_reaches_episode_outside_recent_directory(self) -> None:
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
                "episode_bindings": [
                    {
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
                "episode_bindings": [],
                "episode_links": [],
                "uncertainty": ["planner failed"],
            }

            retrieval = build_plan_retrieval(store, degraded, config(directory))
            assembled = assemble_main_context(
                store, retrieval, 2000, 2000, recent_turns=1
            )

            self.assertIn("蓝绿发布", assembled["recent_conversation"])
            self.assertIn("Sakana", assembled["owner_preferences"])
            store.close()

    def test_raw_detail_requires_explicit_conversation_read(
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
                store, "蓝色保温杯 第三个纸箱", 3, 1000, 1000
            )
            self.assertIn("summary_quality: empty", recalled)
            self.assertNotIn("聊过家中物品的位置", recalled)
            self.assertNotIn("蓝色保温杯藏在阁楼第三个纸箱里", recalled)
            self.assertNotIn("matched_raw", recalled)
            self.assertIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                store.conversation_episode("episode-old")["messages"][0]["content"],
            )
            store._db.execute("DELETE FROM episode_message_recall_terms")
            store._db.execute("DELETE FROM episode_recall_terms")
            store._db.commit()
            store.close()

            reopened = Store(Path(directory) / "momoi.sqlite3")
            self.assertNotIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                recall_episode_context(
                    reopened, "蓝色保温杯 第三个纸箱", 3, 1000, 1000
                ),
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
            memory_id = store._db.execute(
                "SELECT id FROM memories WHERE key='project.mail.waiting'"
            ).fetchone()[0]
            store._db.execute(
                """INSERT INTO memory_conflicts
                   (kind, key, existing_memory_id, candidate_content,
                    source_event_id, evidence_quote, importance, status,
                    created_at, updated_at)
                   VALUES ('episodic', 'project.mail.waiting', ?, '项目邮件已经到达',
                           'correction', '邮件已经到了', 0.8, 'open', ?, ?)""",
                (memory_id, now, now),
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

            self.assertEqual(
                [item["key"] for item in retrieval["confirmed_memories"]],
                ["project.mail.waiting"],
            )
            self.assertEqual([item["id"] for item in retrieval["goals"]], ["goal-mail"])
            self.assertEqual(
                [item["id"] for item in retrieval["reminders"]],
                ["reminder-mail"],
            )
            rendered = "\n".join(assembled.values())
            self.assertNotIn("项目邮件还没到", rendered)
            self.assertNotIn("较早的项目邮件仍在等待", rendered)
            self.assertIn("项目邮件关系到当前合作", rendered)
            self.assertIn("项目邮件已经到达", rendered)
            self.assertIn("goal-mail", rendered)
            self.assertNotIn("微博上有只猫", rendered)
            self.assertNotIn("goal-social", rendered)
            self.assertNotIn("reminder-social", rendered)
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

    def test_memory_budget_round_robins_across_intent_units(self) -> None:
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
                    "recall_queries": ["微博"],
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
                        "recall_queries": ["天气"],
                    },
                    {
                        "id": "music",
                        "event_ids": ["current"],
                        "text": "音乐",
                        "intent": "music",
                        "references": [],
                        "recall_queries": ["音乐"],
                    },
                ]
            )
            split_plan["episode_bindings"][0]["unit_ids"].extend(
                ["social", "weather", "music"]
            )

            retrieval = build_plan_retrieval(
                store, split_plan, config(directory, memory_results=2)
            )
            memories = retrieval["confirmed_memories"]
            self.assertEqual(len(memories), 4)
            self.assertEqual(
                {unit for item in memories for unit in item["unit_ids"]},
                {"mail", "social", "weather", "music"},
            )
            store.close()
