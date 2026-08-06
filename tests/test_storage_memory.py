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
from momoi.memory_tools import MemoryTools
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ToolCall,
    TurnDraft,
)
from momoi.runtime import (
    MomoiDaemon,
)
from momoi.storage import Store
from momoi.storage.scheduling import next_schedule_at


class StorageMemoryTest(unittest.TestCase):
    def test_long_recall_queries_ignore_single_term_episode_and_reflection_hits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("公司副本与风格", episode_id="style-episode")
            self.assertEqual(
                store.search_episodes(
                    "公司 今天 早晨 通勤 疲惫 睡眠 状态 心情 工作 键盘 电池 饭后 "
                    "备用 充电 邮件 微博 猫 游戏 提示词",
                    3,
                ),
                [],
            )

            now = time.time()
            with store._db:
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:noise', '2030-01-01', 'completed', ?, ?, ?)""",
                    (now, now, now),
                )
                store._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', 'interaction.style_noise',
                               '避免堆叠游戏术语', '公司副本', 0.8,
                               'reflection:noise', ?, ?)""",
                    (now, now),
                )
            self.assertEqual(
                store.search_reflection_memories(
                    "公司 今天 早晨 通勤 疲惫 睡眠 状态 心情 工作 键盘 电池 饭后 "
                    "备用 充电 邮件 微博 猫 提示词",
                    3,
                ),
                [],
            )
            self.assertEqual(
                store.search_reflection_memories("游戏术语", 3)[0]["key"],
                "interaction.style_noise",
            )
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
            episode_id = str(store.list_episode_candidates()[0]["id"])
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

    def test_episode_metadata_updates_and_owner_focus_ages_out(self) -> None:
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
                    "episode_bindings": [
                        {
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
            self.assertEqual(store.episode("episode-a")["topics"], ["fresh_marker"])
            self.assertEqual(store.search_episodes("obsolete_marker", 3), [])

            commit("turn-b", "episode-b", "第二主题")
            self.assertEqual(store.episode("episode-a")["status"], "closing")
            commit("turn-c", "episode-c", "第三主题")
            self.assertEqual(store.episode("episode-a")["status"], "closed")
            self.assertEqual(store.episode("episode-b")["status"], "closing")
            self.assertEqual(
                [item["id"] for item in store.list_episode_candidates()],
                ["episode-c", "episode-b"],
            )
            store.close()

    def test_reminder_create_contract_requires_exactly_one_timing_mode(self) -> None:
        spec = next(
            spec for spec in AGENDA_TOOL_SPECS if spec["name"] == "reminder_create"
        )
        self.assertEqual(
            spec["input_schema"]["oneOf"],
            [{"required": ["fire_at"]}, {"required": ["schedule"]}],
        )
        self.assertIn("calls together in one response", AGENDA_TOOL_POLICY)

    def test_legacy_outbox_migrates_to_typed_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            database = sqlite3.connect(path)
            database.execute(
                """CREATE TABLE outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    possible_duplicate INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                )"""
            )
            database.execute(
                "INSERT INTO outbox(turn_id, dedupe_key, text) VALUES ('old', 'old:0', '旧消息')"
            )
            database.commit()
            database.close()

            store = Store(path)
            row = store.due_outbox()[0]
            self.assertEqual(row.kind, "text")
            self.assertIsNone(row.media_path)
            self.assertEqual(row.text, "旧消息")
            store.close()

    def test_legacy_messages_gain_turn_id_without_losing_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            database = sqlite3.connect(path)
            database.execute(
                """CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    source_event_ids_json TEXT NOT NULL
                )"""
            )
            database.execute(
                """CREATE TABLE outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    possible_duplicate INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                )"""
            )
            database.execute(
                """INSERT INTO messages
                   (role, content, created_at, source_event_ids_json)
                   VALUES ('user', '旧对话', 1, '["old-event"]')"""
            )
            database.execute(
                """INSERT INTO messages
                   (role, content, created_at, source_event_ids_json)
                   VALUES ('assistant', '无法证明送达的旧回复', 1, '["old-event"]')"""
            )
            database.execute(
                """INSERT INTO messages
                   (role, content, created_at, source_event_ids_json)
                   VALUES ('assistant', '有出站证据的旧回复', 1, '["old-event"]')"""
            )
            database.execute(
                """INSERT INTO outbox
                   (turn_id, dedupe_key, text, state)
                   VALUES ('old-turn', 'old-turn:0', '有出站证据的旧回复', 'sent')"""
            )
            database.commit()
            database.close()

            store = Store(path)
            columns = {
                row["name"] for row in store._db.execute("PRAGMA table_info(messages)")
            }
            self.assertIn("turn_id", columns)
            row = store._db.execute(
                "SELECT content, turn_id FROM messages WHERE role='user'"
            ).fetchone()
            self.assertEqual(row["content"], "旧对话")
            self.assertTrue(row["turn_id"])
            assistants = {
                row["content"]: (row["delivery_state"], row["outbox_id"])
                for row in store._db.execute(
                    """SELECT content, delivery_state, outbox_id FROM messages
                       WHERE role='assistant'"""
                ).fetchall()
            }
            self.assertEqual(assistants["无法证明送达的旧回复"], ("uncertain", None))
            self.assertEqual(assistants["有出站证据的旧回复"][0], "delivered")
            self.assertIsNotNone(assistants["有出站证据的旧回复"][1])
            episodes = store.search_episodes("旧对话", 3)
            self.assertEqual(len(episodes), 1)
            episode_id = str(episodes[0]["id"])
            self.assertEqual(
                store.conversation_episode(episode_id)["messages"][0]["content"],
                "旧对话",
            )
            store.close()

            reopened = Store(path)
            row = reopened._db.execute(
                "SELECT content, turn_id FROM messages WHERE role='user'"
            ).fetchone()
            self.assertEqual(row["content"], "旧对话")
            self.assertTrue(row["turn_id"])
            self.assertEqual(
                reopened._db.execute(
                    "SELECT COUNT(*) FROM conversation_episodes"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(reopened.search_episodes("旧对话", 3)[0]["id"], episode_id)
            self.assertEqual(
                reopened._db.execute(
                    """SELECT delivery_state FROM messages
                       WHERE content='无法证明送达的旧回复'"""
                ).fetchone()[0],
                "uncertain",
            )
            reopened.close()

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
            recalled = store.save_context_retrieval("turn-1", 1, {"memory_ids": [7]})
            self.assertEqual(recalled["state"], "recalled")
            self.assertEqual(recalled["retrieval"], {"memory_ids": [7]})
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
            social = store.create_episode(
                "微博浏览", episode_id="episode-social", topics=["微博"]
            )
            self.assertEqual(mail["topics"], ["邮件"])
            self.assertEqual(
                [item["id"] for item in store.list_episode_candidates()],
                ["episode-mail", "episode-social"],
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
            self.assertTrue(store.supersede_context_plan("turn-1", 2))
            self.assertIsNone(store.context_plan("turn-1"))
            store.close()

            reopened = Store(path)
            self.assertEqual(reopened.context_plan("turn-1", 2)["state"], "superseded")
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

    def test_conversation_read_pages_back_through_a_long_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("很长的旧对话", episode_id="long-episode")
            for ordinal in range(1, 5):
                turn_id = f"long-turn-{ordinal}"
                store.begin_turn(turn_id, "autonomous", [turn_id])
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
                    "conversation_read",
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
                    "conversation_read",
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

    def test_conversation_read_continues_inside_one_oversized_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("单条超长消息", episode_id="oversized")
            turn_id = "oversized-turn"
            store.begin_turn(turn_id, "autonomous", [turn_id])
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
                ToolCall("first", "conversation_read", {"episode_id": "oversized"}),
                [],
                TurnDraft(),
            )["episode"]["messages"][0]
            self.assertIsNotNone(first["next_content_offset"])
            second = MemoryTools(store).execute(
                ToolCall(
                    "second",
                    "conversation_read",
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

    def test_emotion_paths_are_relative_and_old_workspace_paths_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            emotion = workspace / "emotion"
            emotion.mkdir(parents=True)
            asset = emotion / "asset.gif"
            asset.write_bytes(b"gif")
            database = workspace / "data" / "momoi.sqlite3"
            database.parent.mkdir()
            old = "/old/momoi/config/emotion/asset.gif"

            store = Store(database, workspace)
            now = time.time()
            store._db.execute(
                """INSERT INTO emotions(slug, path, description, created_at, updated_at)
                   VALUES ('salute', ?, '敬礼', ?, ?)""",
                (old, now, now),
            )
            payload = json.dumps(
                {
                    "action": "message",
                    "segments": [{"type": "image", "data": {"file": old}}],
                }
            )
            store._db.execute(
                """INSERT INTO outbox
                   (turn_id, dedupe_key, text, state, attempts, last_error,
                    kind, media_path, payload_json)
                   VALUES ('turn', 'emotion', 'emotion://salute', 'failed', 1,
                           'media asset cannot be read: FileNotFoundError',
                           'image', ?, ?)""",
                (old, payload),
            )
            store._db.commit()
            store.close()

            migrated = Store(database, workspace)
            raw_path = migrated._db.execute(
                "SELECT path FROM emotions WHERE slug='salute'"
            ).fetchone()[0]
            raw_outbox = migrated._db.execute(
                "SELECT state, media_path, payload_json FROM outbox WHERE dedupe_key='emotion'"
            ).fetchone()
            self.assertEqual(raw_path, "emotion/asset.gif")
            self.assertEqual(raw_outbox["media_path"], "emotion/asset.gif")
            self.assertIn("emotion/asset.gif", raw_outbox["payload_json"])
            self.assertEqual(raw_outbox["state"], "pending")
            due = migrated.due_outbox()[0]
            self.assertEqual(due.media_path, str(asset.resolve()))
            self.assertEqual(
                due.payload["segments"][0]["data"]["file"], str(asset.resolve())
            )
            migrated.close()

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

    def test_legacy_context_manifest_tables_are_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            database = sqlite3.connect(path)
            database.executescript(
                "CREATE TABLE context_manifests(id INTEGER);"
                "INSERT INTO context_manifests VALUES (1);"
                "CREATE TABLE context_blobs(id INTEGER);"
            )
            database.close()

            store = Store(path)
            count = store._db.execute(
                "SELECT COUNT(*) FROM context_manifests"
            ).fetchone()[0]
            self.assertEqual(count, 1)
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
            self.assertIn("turn-crash", recovered.open_reconciliations_context())
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
            self.assertEqual(recovered.open_reconciliations_context(), "")
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
            self.assertEqual(recovered.open_reconciliations_context(), "")
            recovered.close()

    def test_owner_can_resolve_or_resume_open_reconciliation_by_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
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
            self.assertEqual(daemon.store.open_reconciliations_context(), "")
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
                            datetime.now().astimezone() + timedelta(milliseconds=20)
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
                            datetime.now().astimezone() + timedelta(hours=1)
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
                        "owner_notify",
                        {
                            "messages": ["检查完成", "目前正常"],
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
            episode = store.search_episodes("检查任务 本次检查正常", 3)[0]
            archived = store.conversation_episode(str(episode["id"]))["messages"]
            self.assertTrue(
                any(
                    "AUTONOMOUS GOAL REVIEW RECORD" in item["content"]
                    for item in archived
                )
            )
            self.assertTrue(any("检查完成" in item["content"] for item in archived))
            store.close()

    def test_one_time_reminder_fires_once_and_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage("qq:1:reminder", "reminder", "提醒我活动", 1, 1)
            store.add_event(event)
            draft = TurnDraft()
            created = tools.execute(
                ToolCall(
                    "reminder-create",
                    "reminder_create",
                    {
                        "text": "该起来活动一下啦",
                        "fire_at": (
                            datetime.now().astimezone() + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )
            self.assertTrue(created["ok"])
            reminder_id = created["reminder"]["id"]
            store.commit_turn([event], event.text, AgentReply(["好"]), draft)
            time.sleep(0.03)
            claimed = store.claim_due_reminder()
            self.assertEqual(claimed["id"], reminder_id)
            self.assertTrue(store.fire_reminder(reminder_id))
            self.assertFalse(store.fire_reminder(reminder_id))
            first = store.due_outbox()[0]
            store.mark_sent(first.id)
            reminder_outbox = store.due_outbox()[0]
            self.assertEqual(reminder_outbox.text, "该起来活动一下啦")
            reminder_message = store._db.execute(
                """SELECT turn_id FROM messages
                   WHERE content='该起来活动一下啦' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            self.assertEqual(reminder_message["turn_id"], reminder_outbox.turn_id)
            self.assertEqual(store.reminder(reminder_id)["status"], "fired")
            reminder_episode = store.search_episodes("起来活动", 3)[0]
            self.assertIn(
                "该起来活动一下啦",
                store.conversation_episode(str(reminder_episode["id"]))["messages"][0][
                    "content"
                ],
            )

            cancel_event = IncomingMessage(
                "qq:1:cancel-reminder", "cancel-reminder", "取消提醒", 2, 2
            )
            store.add_event(cancel_event)
            create_cancelled = TurnDraft()
            pending = tools.execute(
                ToolCall(
                    "reminder-create-2",
                    "reminder_create",
                    {
                        "text": "这条不该发送",
                        "fire_at": (
                            datetime.now().astimezone() + timedelta(hours=1)
                        ).isoformat(),
                    },
                ),
                create_cancelled,
                authority="owner",
                source_event_id=cancel_event.event_id,
                allow_notify=False,
            )["reminder"]
            self.assertTrue(
                tools.execute(
                    ToolCall(
                        "reminder-cancel",
                        "reminder_cancel",
                        {"reminder_id": pending["id"]},
                    ),
                    create_cancelled,
                    authority="owner",
                    source_event_id=cancel_event.event_id,
                    allow_notify=False,
                )["ok"]
            )
            store.commit_turn(
                [cancel_event],
                cancel_event.text,
                AgentReply(["取消了"]),
                create_cancelled,
            )
            self.assertEqual(store.reminder(pending["id"])["status"], "cancelled")
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

    def test_old_default_self_state_migrates_to_neutral_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store._db.execute(
                """UPDATE self_state
                   SET mood_state='cheerful', mood_intensity=0.55,
                       mood_cause='personality baseline',
                       activity='自由安排自己的时间'
                   WHERE id=1"""
            )
            store._db.commit()
            store.close()

            store = Store(path)
            state = store.self_state()
            self.assertEqual(state["mood_state"], "calm")
            self.assertEqual(state["mood_intensity"], 0.35)
            self.assertEqual(state["mood_cause"], "resting baseline")
            self.assertEqual(state["activity"], "spending time freely")
            store.close()

    def test_recurring_reminder_fires_multiple_occurrences(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("momoi.storage.store.time.time", return_value=1000),
        ):
            store = Store(Path(directory) / "momoi.sqlite3")
            tools = AgendaTools(store)
            event = IncomingMessage("qq:1:repeat", "repeat", "每分钟提醒", 1, 1)
            store.add_event(event)
            draft = TurnDraft()
            reminder = tools.execute(
                ToolCall(
                    "reminder-repeat",
                    "reminder_create",
                    {
                        "text": "喝口水啦",
                        "schedule": {
                            "kind": "interval",
                            "every_seconds": 60,
                            "timezone": "Asia/Shanghai",
                        },
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )["reminder"]
            store.commit_turn([event], event.text, AgentReply(["好"]), draft)

            with patch("momoi.storage.store.time.time", return_value=1061):
                self.assertEqual(store.claim_due_reminder()["id"], reminder["id"])
                self.assertTrue(store.fire_reminder(reminder["id"]))
            with patch("momoi.storage.store.time.time", return_value=1122):
                self.assertEqual(store.claim_due_reminder()["id"], reminder["id"])
                self.assertTrue(store.fire_reminder(reminder["id"]))

            rows = store._db.execute(
                "SELECT dedupe_key FROM outbox WHERE turn_id LIKE 'reminder:%' ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["dedupe_key"], rows[1]["dedupe_key"])
            self.assertEqual(store.reminder(reminder["id"])["status"], "pending")
            store.close()

    def test_recurring_reminder_waits_until_quiet_hours_end(self) -> None:
        zone = ZoneInfo("Asia/Shanghai")
        due = datetime(2026, 7, 21, 23, 30, tzinfo=zone).timestamp()
        quiet_end = datetime(2026, 7, 22, 8, 0, tzinfo=zone).timestamp()
        policy = NotificationConfig(
            timezone="Asia/Shanghai", quiet_start="23:00", quiet_end="08:00"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("momoi.storage.store.time.time", return_value=due),
        ):
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """INSERT INTO reminders
                   (id, text, source_event_id, status, fire_at, schedule_json,
                    created_at, updated_at)
                   VALUES ('quiet-repeat', '喝水', 'test', 'pending', ?, ?, ?, ?)""",
                (
                    due,
                    json.dumps(
                        {
                            "kind": "interval",
                            "every_seconds": 3600,
                            "timezone": "Asia/Shanghai",
                        }
                    ),
                    due,
                    due,
                ),
            )
            store._db.commit()
            self.assertIsNotNone(store.claim_due_reminder())
            self.assertFalse(store.fire_reminder("quiet-repeat", policy))
            self.assertEqual(store.reminder("quiet-repeat")["fire_at"], quiet_end)
            self.assertEqual(store.due_outbox(), [])
            with patch("momoi.storage.store.time.time", return_value=quiet_end):
                self.assertIsNotNone(store.claim_due_reminder())
                self.assertTrue(store.fire_reminder("quiet-repeat", policy))
            self.assertEqual(store.due_outbox()[0].text, "喝水")
            store.close()

    def test_heartbeat_has_no_daily_evaluation_cap(self) -> None:
        now = datetime(2026, 7, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        heartbeat = HeartbeatConfig(
            enabled=True,
            initial_delay_seconds=60,
            min_interval_seconds=60,
            max_interval_seconds=600,
        )
        notifications = NotificationConfig(timezone="Asia/Shanghai")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("momoi.storage.store.time.time", return_value=now),
        ):
            store = Store(Path(directory) / "momoi.sqlite3")
            self.assertEqual(store.self_state()["next_heartbeat_at"], 0)
            store.ensure_heartbeat(heartbeat, now)
            self.assertEqual(store.next_heartbeat_due_at(True), now + 60)
            for index in range(20):
                turn_id = f"heartbeat-{index}"
                store.begin_turn(turn_id, "autonomous", [f"heartbeat:{index}"])
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
            episode = store.search_episodes("关卡灵感 点子", 3)[0]
            self.assertEqual(len(store.episode_turns(str(episode["id"]))), 20)
            self.assertIn(
                "AUTONOMOUS HEARTBEAT RECORD",
                store.conversation_episode(str(episode["id"]))["messages"][-1][
                    "content"
                ],
            )
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
                    expects_reply=True,
                    reply_expectation="主人对晚餐的选择",
                ),
                turn_id="owner-question",
                target_channel="weixin",
            )
            row = store.due_outbox()[0]
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(row.id, 60))
            self.assertEqual(
                store.pending_owner_reply(1000)["expected_response"],
                "主人对晚餐的选择",
            )
            self.assertEqual(store.next_heartbeat_due_at(False), 1060)
            self.assertIsNotNone(
                store.claim_due_heartbeat(heartbeat, NotificationConfig(), now=1060)
            )

            store.begin_turn("reply-check", "autonomous", ["heartbeat:1060"])
            with patch("momoi.storage.store.time.time", return_value=1060):
                store.commit_heartbeat(
                    "reply-check",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    activity="等主人选晚餐",
                    result="轻轻问了一次",
                    next_heartbeat_at=1660,
                    mood_update=None,
                    messages=["还没想好的话，我可以帮你挑两个呀。"],
                    reason="晚餐选择还需要主人回复",
                    reply_expectation="主人是否需要帮忙挑晚餐",
                    pending_reply_turn_id="owner-question",
                    continue_waiting_for_reply=True,
                    reply_initial_interval_seconds=60,
                )
            self.assertEqual(store.next_heartbeat_due_at(False), 1180)
            pending = store.pending_owner_reply(1060)
            self.assertEqual(pending["expected_response"], "主人对晚餐的选择")
            self.assertEqual(pending["heartbeat_checks"], 1)
            key = store._db.execute(
                "SELECT notification_key FROM notifications WHERE turn_id='reply-check'"
            ).fetchone()[0]
            self.assertEqual(key, "heartbeat.reply_followup")

            stale_followup = store.due_outbox()[0]
            self.assertEqual(stale_followup.channel, "weixin")
            self.assertEqual(
                store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?",
                    (stale_followup.id,),
                ).fetchone()[0],
                "主人是否需要帮忙挑晚餐",
            )
            with patch("momoi.storage.delivery.time.time", return_value=1070):
                self.assertTrue(store.mark_sent(stale_followup.id, 60))
            pending = store.pending_owner_reply(1070)
            self.assertEqual(pending["expected_response"], "主人是否需要帮忙挑晚餐")
            self.assertEqual(pending["heartbeat_checks"], 1)
            self.assertEqual(pending["delivered_followups"], 1)
            self.assertEqual(store.next_heartbeat_due_at(False), 1180)

            answer = IncomingMessage("owner-answer", "answer", "吃面", 1061, 1061)
            store.add_event(answer)
            self.assertIsNone(store.pending_owner_reply(1071))
            self.assertIsNone(store.next_heartbeat_due_at(False))
            self.assertEqual(store.next_heartbeat_due_at(True), 1660)
            self.assertEqual(
                store._db.execute(
                    "SELECT next_heartbeat_at FROM self_state WHERE id=1"
                ).fetchone()[0],
                1660,
            )
            self.assertFalse(store.mark_sending(stale_followup.id))
            store.commit_turn(
                [answer],
                "吃面",
                AgentReply(
                    ["吃完告诉我呀"],
                    expects_reply=True,
                    reply_expectation="主人是否已经吃完",
                ),
                turn_id="next-question",
                target_channel="weixin",
            )
            with patch("momoi.storage.delivery.time.time", return_value=1080):
                self.assertTrue(store.mark_sent(store.due_outbox()[0].id, 60))
            self.assertEqual(store.next_heartbeat_due_at(False), 1140)
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
                self.assertFalse(store.mark_sent(outbox.id, 60))

            with patch("momoi.storage.delivery.time.time", return_value=1010):
                store.commit_turn(
                    [],
                    "",
                    AgentReply(
                        [],
                        expects_reply=True,
                        reply_expectation="老师想说的后续",
                    ),
                    turn_id="live-reply",
                    reply_initial_delay=75,
                )

            self.assertEqual(
                store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?", (outbox.id,)
                ).fetchone()[0],
                "老师想说的后续",
            )
            pending = store.pending_owner_reply(1010)
            self.assertEqual(pending["expected_response"], "老师想说的后续")
            self.assertEqual(store.next_heartbeat_due_at(False), 1085)
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
                    expects_reply=True,
                    reply_expectation="老师的选择",
                ),
                turn_id="queued-live-reply",
            )
            self.assertIsNone(store.pending_owner_reply())

            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(store.due_outbox()[0].id, 60))
            self.assertEqual(store.next_heartbeat_due_at(False), 1060)
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
                        expects_reply=True,
                        reply_expectation="不存在的消息",
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
                    expects_reply=True,
                    reply_expectation="主人的风险偏好",
                ),
                turn_id="question",
            )
            store._db.execute("UPDATE self_state SET next_heartbeat_at=4900 WHERE id=1")
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(store.due_outbox()[0].id, 60))
            state = store.self_state()
            self.assertEqual(state["next_heartbeat_at"], 4900)
            self.assertEqual(state["pending_reply_next_check_at"], 1060)

            store.add_event(
                IncomingMessage("answer", "answer", "我喜欢求稳", 1020, 1020)
            )
            state = store.self_state()
            self.assertIsNone(state["pending_reply_next_check_at"])
            self.assertEqual(store.next_heartbeat_due_at(True), 4900)
            store.close()

    def test_heartbeat_can_stop_reply_annealing_without_owner_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """UPDATE self_state SET pending_reply_turn_id='question',
                   pending_reply_expectation='主人是否愿意继续聊',
                   pending_reply_since=1000, pending_reply_checks=2,
                   next_heartbeat_at=1100 WHERE id=1"""
            )
            store.begin_turn("stop-waiting", "autonomous", ["heartbeat:1100"])
            with patch("momoi.storage.store.time.time", return_value=1100):
                store.commit_heartbeat(
                    "stop-waiting",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    activity="做自己的事",
                    result="决定不再等待",
                    next_heartbeat_at=1700,
                    mood_update=None,
                    messages=[],
                    reason="这段等待已经自然结束",
                    pending_reply_turn_id="question",
                    continue_waiting_for_reply=False,
                )
            self.assertIsNone(store.pending_owner_reply(1100))
            self.assertEqual(store.next_heartbeat_due_at(True), 1700)
            store.close()

    def test_reply_attention_returns_to_ordinary_rhythm_after_three_checks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """UPDATE self_state SET pending_reply_turn_id='question',
                   pending_reply_expectation='主人是否愿意继续聊',
                   pending_reply_since=1000, pending_reply_checks=2,
                   next_heartbeat_at=1100 WHERE id=1"""
            )
            store.begin_turn("third-check", "autonomous", ["heartbeat:1100"])
            with patch("momoi.storage.store.time.time", return_value=1100):
                store.commit_heartbeat(
                    "third-check",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    activity="等主人回复",
                    result="继续等待",
                    next_heartbeat_at=2000,
                    mood_update=None,
                    messages=[],
                    reason="仍然想听主人回答",
                    pending_reply_turn_id="question",
                    continue_waiting_for_reply=True,
                )
            self.assertEqual(store.pending_owner_reply(1100)["heartbeat_checks"], 3)
            self.assertEqual(store.next_heartbeat_due_at(False), 2000)
            store.close()

    def test_new_owner_event_suppresses_heartbeat_visible_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            revision = int(
                store.heartbeat_conversation_snapshot()["owner_event_revision"]
            )
            store.begin_turn("stale-heartbeat", "autonomous", ["heartbeat:1000"])
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
            store.begin_turn("heartbeat-live", "autonomous", ["heartbeat:1000"])
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
            store.begin_turn("cooldown-heartbeat", "autonomous", ["heartbeat:1100"])
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

    def test_owner_message_supersedes_queued_heartbeat_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.begin_turn("heartbeat-chat", "autonomous", ["heartbeat:1000"])
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

    def test_old_notification_schema_migrates_and_drops_stale_heartbeat_chat(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            Store(path).close()
            database = sqlite3.connect(path)
            database.execute("DROP INDEX notifications_due")
            database.execute("ALTER TABLE notifications RENAME TO notifications_new")
            database.execute(
                """CREATE TABLE notifications (
                       id TEXT PRIMARY KEY,
                       turn_id TEXT NOT NULL UNIQUE,
                       goal_id TEXT NOT NULL,
                       notification_key TEXT NOT NULL,
                       priority TEXT NOT NULL CHECK (priority IN ('normal', 'urgent')),
                       reason TEXT NOT NULL,
                       messages_json TEXT NOT NULL,
                       reply_expectation TEXT NOT NULL DEFAULT '',
                       state TEXT NOT NULL CHECK (state IN ('pending', 'queued')),
                       not_before REAL NOT NULL,
                       claimed_at REAL,
                       created_at REAL NOT NULL,
                       queued_at REAL,
                       target_channel TEXT NOT NULL DEFAULT ''
                   )"""
            )
            database.execute(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, state, not_before, created_at)
                   VALUES ('stale', 'stale-turn', 'heartbeat', 'heartbeat.chat',
                           'normal', 'old chat', '["旧对话"]', 'pending', 1, 1)"""
            )
            database.execute("DROP TABLE notifications_new")
            database.commit()
            database.close()

            migrated = Store(path)
            notification = migrated._db.execute(
                """SELECT state, superseded_reason FROM notifications
                   WHERE id='stale'"""
            ).fetchone()
            self.assertEqual(notification["state"], "superseded")
            self.assertEqual(
                notification["superseded_reason"],
                "process_restart_invalidated_ephemeral_contact",
            )
            migrated.close()

    def test_reply_attention_never_delays_an_earlier_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.commit_turn(
                [],
                "",
                AgentReply(
                    ["到家了吗？"],
                    expects_reply=True,
                    reply_expectation="主人是否已经到家",
                ),
                turn_id="question",
            )
            store._db.execute("UPDATE self_state SET next_heartbeat_at=1030 WHERE id=1")
            with patch("momoi.storage.delivery.time.time", return_value=1000):
                self.assertTrue(store.mark_sent(store.due_outbox()[0].id, 60))
            self.assertEqual(store.next_heartbeat_due_at(False), 1030)
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
                            "timezone": "Asia/Shanghai",
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
                {"kind": "daily", "at": "08:00", "timezone": "Asia/Shanghai"},
                after.timestamp(),
            )
            self.assertEqual(
                datetime.fromtimestamp(next_daily, ZoneInfo("Asia/Shanghai")),
                datetime(2026, 7, 21, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
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
            store = Store(Path(directory) / "momoi.sqlite3")
            quiet = NotificationConfig(
                timezone="Asia/Shanghai",
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
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(2030, 1, 2, 10, 0, tzinfo=zone).timestamp()
            pending = IncomingMessage("qq:pending", "pending", "主人消息", now, now)
            store.add_event(pending)
            add(store, "normal", "normal.status", "normal", now)
            add(store, "urgent", "urgent.failure", "urgent", now)
            policy = NotificationConfig(
                timezone="Asia/Shanghai",
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

    def test_legacy_continuity_migrates_once_to_a_recallable_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            store.close()
            database = sqlite3.connect(path)
            database.execute(
                """CREATE TABLE continuity_state (
                       id INTEGER PRIMARY KEY,
                       content TEXT NOT NULL,
                       source_event_ids_json TEXT NOT NULL,
                       updated_at REAL NOT NULL
                   )"""
            )
            database.execute(
                """INSERT INTO continuity_state
                   VALUES (1, '{"topic":"当前话题","open_loops":["等待结果"]}',
                           '["old-event"]', 10)"""
            )
            database.commit()
            database.close()

            store = Store(path)
            episode = store.search_episodes("当前话题 等待结果", 3)[0]
            self.assertEqual(episode["status"], "closing")
            self.assertIn("Imported legacy continuity", episode["working_summary"])
            self.assertFalse(store._table_exists("continuity_state"))
            episode_id = episode["id"]
            store.close()

            reopened = Store(path)
            self.assertEqual(
                reopened.search_episodes("当前话题 等待结果", 3)[0]["id"],
                episode_id,
            )
            reopened.close()

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
                store.memory_context("灯光按我喜欢的来", 6, 8000),
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
            recalled = store.memory_context("卧室灯光", 6, 8000)
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
                store.memory_context("卧室灯光", 6, 8000),
            )
            store.close()

    def test_uncertain_memory_conflict_waits_for_owner_confirmation(self) -> None:
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
            self.assertIn("candidate=主人喜欢冷色灯", store.memory_conflicts_context())

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
            self.assertEqual(store.memory_conflicts_context(), "")
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
            self.assertTrue(store.memory_conflicts_context())

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
            self.assertEqual(store.search_memories("灯光颜色", 6), [])
            self.assertEqual(store.memory_conflicts_context(), "")
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
            self.assertIn("主人喜欢冷色", store.memory_context("灯光颜色", 6, 1000))
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
            ]
            for index, (arguments, error) in enumerate(invalid):
                result = tools.execute(
                    ToolCall(f"invalid-{index}", "memory_remember", arguments),
                    [event],
                    TurnDraft(),
                )
                self.assertEqual(result["error"], error)
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
                        },
                    ),
                    [event],
                    draft,
                )
                self.assertTrue(result["ok"])
                store.commit_turn([event], event.text, AgentReply(["记住了"]), draft)

            always = store.always_memory_context()
            recent = store.recent_memory_context(500)
            recalled = store.memory_context("骑车", 6, 1000)
            self.assertIn("波浪号", always)
            self.assertNotIn("骑车", always)
            self.assertIn("恢复身体", recent)
            self.assertNotIn("波浪号", recent)
            self.assertIn("喜欢骑车", recalled)
            self.assertNotIn("波浪号", recalled)
            self.assertNotIn("恢复身体", recalled)
            self.assertEqual(always.count("波浪号"), 1)
            store.close()

    def test_legacy_summaries_become_closed_episodes_without_losing_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            database = sqlite3.connect(path)
            database.execute(
                """CREATE TABLE messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       role TEXT NOT NULL,
                       content TEXT NOT NULL,
                       created_at REAL NOT NULL,
                       source_event_ids_json TEXT NOT NULL
                   )"""
            )
            database.executemany(
                """INSERT INTO messages
                   (role, content, created_at, source_event_ids_json)
                   VALUES (?, ?, ?, '[]')""",
                [
                    ("user", "项目邮件还没到", 1),
                    ("assistant", "我会记着项目邮件", 1),
                    ("user", "微博上看到一只猫", 2),
                    ("assistant", "那只猫很可爱", 2),
                ],
            )
            database.execute(
                """CREATE TABLE conversation_summaries (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       start_message_id INTEGER NOT NULL,
                       end_message_id INTEGER NOT NULL UNIQUE,
                       content TEXT NOT NULL,
                       created_at REAL NOT NULL
                   )"""
            )
            database.execute(
                """INSERT INTO conversation_summaries
                   (start_message_id, end_message_id, content, created_at)
                   VALUES (1, 2, '较早的项目邮件仍在等待', 3)"""
            )
            database.commit()
            database.close()

            store = Store(path)
            self.assertFalse(store._table_exists("conversation_summaries"))
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM messages WHERE turn_id<>''"
                ).fetchone()[0],
                4,
            )
            summary_episode = store.search_episodes("项目邮件 等待", 3)[0]
            self.assertEqual(summary_episode["status"], "closed")
            self.assertIn("UNVERIFIED legacy summary", summary_episode["summary"])
            self.assertIn("较早的项目邮件仍在等待", summary_episode["summary"])
            raw_episode = store.search_episodes("微博 猫", 3)[0]
            self.assertEqual(raw_episode["status"], "open")
            self.assertIn(
                "Imported extractive recall index", raw_episode["working_summary"]
            )
            self.assertEqual(
                [
                    item["content"]
                    for item in store.conversation_episode(str(raw_episode["id"]))[
                        "messages"
                    ]
                ],
                ["微博上看到一只猫", "那只猫很可爱"],
            )
            search = MemoryTools(store).execute(
                ToolCall(
                    "conversation-search",
                    "conversation_search",
                    {"query": "项目邮件 等待"},
                ),
                [],
                TurnDraft(),
            )
            self.assertEqual(search["count"], 1)
            read = MemoryTools(store).execute(
                ToolCall(
                    "conversation-read",
                    "conversation_read",
                    {"episode_id": search["results"][0]["id"]},
                ),
                [],
                TurnDraft(),
            )
            self.assertTrue(read["ok"])
            self.assertEqual(len(read["episode"]["messages"]), 2)
            episode_ids = {
                row["id"]
                for row in store._db.execute(
                    "SELECT id FROM conversation_episodes"
                ).fetchall()
            }
            store.close()

            reopened = Store(path)
            self.assertEqual(
                {
                    row["id"]
                    for row in reopened._db.execute(
                        "SELECT id FROM conversation_episodes"
                    ).fetchall()
                },
                episode_ids,
            )
            self.assertEqual(
                reopened._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                4,
            )
            reopened.close()

    def test_legacy_raw_turns_are_chunked_and_indexed_without_raw_injection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            database = sqlite3.connect(path)
            database.execute(
                """CREATE TABLE messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       role TEXT NOT NULL,
                       content TEXT NOT NULL,
                       created_at REAL NOT NULL,
                       source_event_ids_json TEXT NOT NULL
                   )"""
            )
            rows = []
            for index in range(25):
                rows.extend(
                    [
                        ("user", f"第{index + 1}个旧主题", index, "[]"),
                        ("assistant", f"第{index + 1}个旧回复", index, "[]"),
                    ]
                )
            database.executemany(
                """INSERT INTO messages
                   (role, content, created_at, source_event_ids_json)
                   VALUES (?, ?, ?, ?)""",
                rows,
            )
            database.commit()
            database.close()

            store = Store(path)
            episodes = store._db.execute(
                """SELECT id, working_summary, summarized_through_ordinal
                   FROM conversation_episodes WHERE status='open'
                   ORDER BY created_at"""
            ).fetchall()
            self.assertEqual(len(episodes), 2)
            self.assertEqual(
                [row["summarized_through_ordinal"] for row in episodes], [0, 0]
            )
            self.assertTrue(
                all(
                    str(row["working_summary"]).startswith(
                        "[Imported extractive recall index;"
                    )
                    for row in episodes
                )
            )
            candidate = store.claim_episode_annealing_candidate(2, 10000)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["through_ordinal"], 22)
            self.assertTrue(candidate["messages"])
            store.release_episode_annealing(str(candidate["episode"]["id"]))
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 50
            )
            store.close()
