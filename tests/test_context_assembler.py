import tempfile
import time
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage
from momoi.runtime.context_assembler import (
    assemble_main_context,
    build_plan_retrieval,
    recall_episode_context,
)
from momoi.storage import Store, estimate_tokens


def config(directory: str, memory_results: int = 6) -> AppConfig:
    return AppConfig(
        llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
        channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        system_prompt="test",
        recent_raw_tokens=2000,
        recent_turns=2,
        memory_results=memory_results,
        memory_tokens=4000,
        summary_results=3,
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
                       (kind, key, content, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES ('profile', 'owner.name', '主人的名字是 Sakana',
                               'owner', 'owner-name', '我叫 Sakana', 1, 1, 1)"""
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
            self.assertIn("Sakana", assembled["confirmed_memories"])
            store.close()

    def test_raw_detail_remains_automatically_recallable_after_summary_omits_it(
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
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["我记得这个位置了"]),
                turn_id="rare-turn",
            )
            store.link_turn_to_episode("episode-old", "rare-turn")
            store._db.execute(
                """UPDATE conversation_episodes
                   SET working_summary='聊过家中物品的位置',
                       summarized_through_ordinal=1
                   WHERE id='episode-old'"""
            )

            recalled = recall_episode_context(
                store, "蓝色保温杯 第三个纸箱", 3, 1000, 1000
            )
            self.assertIn("聊过家中物品的位置", recalled)
            self.assertIn("蓝色保温杯藏在阁楼第三个纸箱里", recalled)
            self.assertIn("matched_raw", recalled)
            store._db.execute("DELETE FROM episode_message_recall_terms")
            store._db.execute("DELETE FROM episode_recall_terms")
            store._db.commit()
            store.close()

            reopened = Store(Path(directory) / "momoi.sqlite3")
            self.assertIn(
                "蓝色保温杯藏在阁楼第三个纸箱里",
                recall_episode_context(
                    reopened, "蓝色保温杯 第三个纸箱", 3, 1000, 1000
                ),
            )
            reopened.close()

    def test_commit_without_a_context_plan_gets_a_searchable_fallback_episode(
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

            episode = store.search_episodes("规划器失败 归档", 3)[0]
            self.assertEqual(
                store.episode_turns(str(episode["id"]))[0]["turn_id"], "fallback"
            )
            self.assertIn(
                "这轮仍然会归档",
                recall_episode_context(store, "规划器失败 归档", 3, 1000, 1000),
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
                store, plan("等待 项目 邮件"), config(directory)
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
            self.assertIn("项目邮件还没到", rendered)
            self.assertIn("较早的项目邮件仍在等待", rendered)
            self.assertIn("项目邮件关系到当前合作", rendered)
            self.assertIn("项目邮件已经到达", rendered)
            self.assertIn("goal-mail", rendered)
            self.assertNotIn("微博上有只猫", rendered)
            self.assertNotIn("goal-social", rendered)
            self.assertNotIn("reminder-social", rendered)
            autonomous = recall_episode_context(store, "等待 项目 邮件", 3, 2000, 2000)
            self.assertIn("较早的项目邮件仍在等待", autonomous)
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
