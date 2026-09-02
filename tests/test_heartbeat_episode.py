import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from momoi.config import NotificationConfig
from momoi.context_time import context_timestamp
from momoi.storage import Store
from momoi.storage.heartbeat import RECENT_HEARTBEAT_LIMIT


class HeartbeatEpisodeTests(unittest.TestCase):
    def _commit(self, store: Store, turn_id: str, now: float) -> str:
        with patch("momoi.storage.heartbeat.time.time", return_value=now):
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
        return str(
            store._db.execute(
                "SELECT episode_id FROM episode_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()["episode_id"]
        )

    def test_heartbeat_uses_one_episode_per_local_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            zone = ZoneInfo("Asia/Shanghai")
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            day_one = datetime(2026, 8, 16, 10, tzinfo=zone).timestamp()
            first = self._commit(store, "heartbeat-1", day_one)
            second = self._commit(store, "heartbeat-2", day_one + 3600)
            next_day = self._commit(store, "heartbeat-3", day_one + 86400)
            self.assertEqual(first, second)
            self.assertNotEqual(first, next_day)
            self.assertEqual(
                store.episode(first)["title"],
                "心跳 · 2026-08-16",
            )
            store.close()

    def test_closed_daily_episode_is_not_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(
                2026, 8, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            closed = self._commit(store, "heartbeat-closed", now)
            store._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', closed_at=? WHERE id=?""",
                (now, closed),
            )
            store._db.commit()
            successor = self._commit(store, "heartbeat-after-close", now + 3600)
            self.assertNotEqual(closed, successor)
            self.assertEqual(store.episode(closed)["status"], "closed")
            self.assertNotEqual(store.episode(successor)["status"], "closed")
            store.close()

    def test_delayed_notification_stays_with_heartbeat_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            zone = ZoneInfo("Asia/Shanghai")
            store = Store(
                Path(directory) / "momoi.sqlite3", timezone="Asia/Shanghai"
            )
            heartbeat_at = datetime(
                2026, 8, 16, 23, 59, tzinfo=zone
            ).timestamp()
            delivered_at = heartbeat_at + 120
            turn_id = "heartbeat-delayed"
            with patch("momoi.storage.heartbeat.time.time", return_value=heartbeat_at):
                store.begin_turn(turn_id, "heartbeat", ["heartbeat:delayed"])
                store.commit_heartbeat(
                    turn_id,
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    activity="刷微博",
                    result="发现一条消息",
                    next_heartbeat_at=heartbeat_at + 60,
                    mood_update=None,
                    messages=[],
                    reason="test",
                )
            episode_id = store._db.execute(
                "SELECT episode_id FROM episode_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()["episode_id"]
            with store._db:
                store._db.execute(
                    """INSERT INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, state, not_before, claimed_at, created_at)
                       VALUES ('delayed', ?, 'heartbeat', 'heartbeat.chat',
                               'normal', 'test', '["有新消息"]', 'pending', ?, ?, ?)""",
                    (turn_id, delivered_at, delivered_at, heartbeat_at),
                )
            with patch("momoi.storage.notifications.time.time", return_value=delivered_at):
                self.assertTrue(store.queue_notification("delayed"))
            linked = store._db.execute(
                "SELECT episode_id FROM episode_turns WHERE turn_id=?", (turn_id,)
            ).fetchall()
            self.assertEqual({str(row["episode_id"]) for row in linked}, {episode_id})
            store.close()


class RecentHeartbeatActivityTests(unittest.TestCase):
    def _commit(
        self,
        store: Store,
        turn_id: str,
        now: float,
        activity: str,
    ) -> None:
        with patch("momoi.storage.heartbeat.time.time", return_value=now):
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

    def test_recent_heartbeat_activities_keep_latest_six(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(
                2026, 8, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            for index in range(RECENT_HEARTBEAT_LIMIT + 1):
                self._commit(
                    store,
                    f"heartbeat-{index}",
                    now + index,
                    f"活动{index}",
                )
            items = store.recent_heartbeat_activities()
            self.assertEqual(
                [item["text"] for item in items],
                [f"活动{index}" for index in range(1, RECENT_HEARTBEAT_LIMIT + 1)],
            )
            self.assertEqual(
                [item["at"] for item in items],
                [
                    context_timestamp(now + index, store.timezone)
                    for index in range(1, RECENT_HEARTBEAT_LIMIT + 1)
                ],
            )
            store.close()

    def test_reply_followup_does_not_append_recent_heartbeat_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = datetime(
                2026, 8, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            self._commit(store, "heartbeat-1", now, "刷微博")
            self.assertEqual(
                [item["text"] for item in store.recent_heartbeat_activities()],
                ["刷微博"],
            )
            store.begin_turn("owner-1", "owner", ["evt-1"])
            with store._db:
                store._db.execute(
                    """UPDATE self_state SET pending_reply_turn_id='owner-1',
                       pending_reply_expectation='x', pending_reply_since=?,
                       pending_reply_next_check_at=? WHERE id=1""",
                    (now, now + 60),
                )
            store.begin_turn(
                "reply-followup", "reply_followup", ["reply-followup:1"]
            )
            with patch("momoi.storage.heartbeat.time.time", return_value=now + 60):
                store.commit_reply_followup(
                    "reply-followup",
                    owner_event_revision=0,
                    notification_config=NotificationConfig(),
                    mood_update=None,
                    reason="test",
                    pending_reply_turn_id="owner-1",
                )
            self.assertEqual(
                [item["text"] for item in store.recent_heartbeat_activities()],
                ["刷微博"],
            )
            store.close()

    def test_recent_heartbeat_activities_read_existing_records(self) -> None:
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
            self.assertEqual(
                [item["text"] for item in store.recent_heartbeat_activities()],
                ["刷微博", "整理笔记", "发呆"],
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
