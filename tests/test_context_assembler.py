import time
import tempfile
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
            self.assertEqual(
                [item["id"] for item in retrieval["goals"]], ["goal-mail"]
            )
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
            autonomous = recall_episode_context(
                store, "等待 项目 邮件", 3, 2000, 2000
            )
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
            split_plan["episode_bindings"][0]["unit_ids"].append("social")

            retrieval = build_plan_retrieval(
                store, split_plan, config(directory, memory_results=2)
            )
            memories = retrieval["confirmed_memories"]
            self.assertEqual(len(memories), 2)
            self.assertEqual(
                {unit for item in memories for unit in item["unit_ids"]},
                {"mail", "social"},
            )
            store.close()
