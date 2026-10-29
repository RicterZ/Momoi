import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from momoi.config.models import NotificationConfig
from momoi.storage.core.timestamps import context_timestamp
from momoi.storage import Store
from momoi.runtime.transcript.building import build_transcript


class HeartbeatEpisodeTests(unittest.TestCase):
    def _commit(self, store: Store, turn_id: str, now: float) -> None:
        with patch("momoi.storage.agenda.heartbeat_commits.time.time", return_value=now):
            store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
            store.commit_heartbeat(
                turn_id,
                owner_event_revision=0,
                notification_config=NotificationConfig(),
                activity="刷微博",
                result=f"记录 {turn_id}",
                next_heartbeat_at=now + 60,
                mood_update=None,
                messages=[],
                reason="test",
            )

    def _pending(self, store: Store) -> set[str]:
        return {
            str(row["id"])
            for row in store._episode_consolidation_pending_rows(50)
        }

    def test_heartbeat_turns_are_consolidated_by_topic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            now = datetime(
                2026, 8, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            self._commit(store, "heartbeat-1", now)
            self._commit(store, "heartbeat-2", now + 3600)
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM episode_turns").fetchone()[0],
                0,
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM conversation_episodes"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(self._pending(store), {"heartbeat-1", "heartbeat-2"})
            store.close()

    def test_pending_notification_defers_consolidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            heartbeat_at = datetime(
                2026, 8, 16, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            delivered_at = heartbeat_at + 120
            turn_id = "heartbeat-delayed"
            self._commit(store, turn_id, heartbeat_at)
            with store._db:
                store._db.execute(
                    """INSERT INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, state, not_before, claimed_at, created_at)
                       VALUES ('delayed', ?, 'heartbeat', 'heartbeat.chat',
                               'normal', 'test', '["有新消息"]', 'pending', ?, ?, ?)""",
                    (turn_id, delivered_at, delivered_at, heartbeat_at),
                )
            self.assertNotIn(turn_id, self._pending(store))
            with patch("momoi.storage.agenda.notifications.time.time", return_value=delivered_at):
                self.assertTrue(store.queue_notification("delayed"))
            self.assertIsNone(
                store._db.execute(
                    "SELECT 1 FROM episode_turns WHERE turn_id=?", (turn_id,)
                ).fetchone()
            )
            with store._db:
                store._db.execute(
                    "UPDATE messages SET delivery_state='delivered' WHERE turn_id=?",
                    (turn_id,),
                )
            self.assertIn(turn_id, self._pending(store))
            store.close()


class HeartbeatTimelineTests(unittest.TestCase):
    def _commit(
        self,
        store: Store,
        turn_id: str,
        now: float,
        activity: str,
    ) -> None:
        with patch("momoi.storage.agenda.heartbeat_commits.time.time", return_value=now):
            store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
            store.commit_heartbeat(
                turn_id,
                owner_event_revision=0,
                notification_config=NotificationConfig(),
                activity=activity,
                result=f"记录 {turn_id}",
                next_heartbeat_at=now + 60,
                mood_update=None,
                messages=[],
                reason="test",
            )

    def test_heartbeat_timeline_retains_activity_and_result_in_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(
                2026, 8, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            for index in range(8):
                self._commit(
                    store,
                    f"heartbeat-{index}",
                    now + index,
                    f"活动{index}",
                )
            items = store.recent_conversation_messages(7, 10000)
            self.assertEqual([item["content"] for item in items], [
                f"Activity: 活动{index}\nResult: 记录 heartbeat-{index}" for index in range(1, 8)
            ])
            self.assertEqual([item["timestamp"] for item in items], [
                context_timestamp(now + index, store.timezone) for index in range(1, 8)
            ])
            transcript = build_transcript(items, timezone=store.timezone)
            self.assertEqual(str(transcript.messages).count('<heartbeat id="H'), 7)
            self.assertNotIn("<bubble>", str(transcript.messages))
            store.close()


    def test_heartbeat_header_migration_preserves_existing_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(
                2026, 8, 16, 17, 6, 36, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            for index, activity in enumerate(["刷微博", "整理笔记", "发呆"], start=1):
                turn_id = f"legacy-heartbeat-{index}"
                record = (
                    "[AUTONOMOUS HEARTBEAT RECORD; not sent to the owner]\n"
                    f"Activity: {activity}\nResult: 看了今天的消息"
                )
                with store._db:
                    store._db.execute(
                        """INSERT INTO turns
                           (id, kind, source_ids_json, state, started_at, updated_at)
                           VALUES (?, 'autonomous', '[]', 'completed', ?, ?)""",
                        (turn_id, now + index, now + index),
                    )
                    store._db.execute(
                        """INSERT INTO messages
                           (turn_id, role, content, created_at,
                            source_event_ids_json, delivery_state)
                           VALUES (?, 'assistant', ?, ?, ?, 'internal')""",
                        (
                            turn_id,
                            record,
                            now + index,
                            json.dumps([f"heartbeat-record:{turn_id}"]),
                        ),
                    )
            with store._db:
                store._db.execute("PRAGMA user_version=4")
            path = Path(directory) / "momoi.sqlite3"
            store.close()
            store = Store(path)
            self.assertEqual(
                [item["content"] for item in store.recent_conversation_messages(10, 10000) if item["role"] == "heartbeat"],
                [f"Activity: {activity}\nResult: 看了今天的消息" for activity in ("刷微博", "整理笔记", "发呆")],
            )
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM turns WHERE workflow_kind='heartbeat'").fetchone()[0], 3)
            store.close()


if __name__ == "__main__":
    unittest.main()
