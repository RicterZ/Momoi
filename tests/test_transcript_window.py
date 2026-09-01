import tempfile
import unittest
from pathlib import Path

from momoi.storage import Store


class TranscriptWindowTest(unittest.TestCase):
    @staticmethod
    def _add_visible_turn(
        store: Store,
        index: int,
        *,
        kind: str = "owner",
        source: str | None = None,
    ) -> str:
        turn_id = f"turn-{index:04d}"
        store.begin_turn(turn_id, kind, [source or turn_id])
        with store._db:
            store._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at,
                    source_event_ids_json, delivery_state)
                   VALUES (?, 'assistant', ?, ?, '[]', 'delivered')""",
                (turn_id, f"message {index}", float(index)),
            )
            store._db.execute(
                """UPDATE turns SET state='completed', updated_at=?
                   WHERE id=?""",
                (float(index), turn_id),
            )
        return turn_id

    def test_window_grows_then_slides_to_low_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            for index in range(1, 101):
                self._add_visible_turn(store, index)

            self.assertEqual(store.transcript_window_turn_limit(48, 96), 48)
            self._add_visible_turn(store, 101)
            self.assertEqual(store.transcript_window_turn_limit(48, 96), 49)
            for index in range(102, 148):
                self._add_visible_turn(store, index)
            self.assertEqual(store.transcript_window_turn_limit(48, 96), 95)
            self._add_visible_turn(store, 148)
            self.assertEqual(store.transcript_window_turn_limit(48, 96), 48)
            store.close()

            reopened = Store(path)
            self.assertEqual(reopened.transcript_window_turn_limit(48, 96), 48)
            reopened.close()

    def test_episode_directory_contains_only_transcript_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.create_episode("当前聊天", episode_id="current")
            store.create_episode("窗口外聊天", episode_id="outside")
            store.create_episode("心跳归档", episode_id="heartbeat")
            current = self._add_visible_turn(store, 1)
            outside = self._add_visible_turn(store, 2)
            heartbeat = self._add_visible_turn(
                store,
                3,
                kind="autonomous",
                source="heartbeat:3",
            )
            store.link_turn_to_episode("current", current)
            store.link_turn_to_episode("outside", outside)
            store.link_turn_to_episode("heartbeat", heartbeat)

            directory_rows = store.episode_directory_for_turns(
                [current, heartbeat],
                exclude_runtime_archives=True,
            )

            self.assertEqual(len(directory_rows), 1)
            self.assertEqual(directory_rows[0]["id"], "current")
            self.assertEqual(directory_rows[0]["title"], "当前聊天")
            self.assertEqual(
                set(directory_rows[0]),
                {"id", "title", "last_activity_timestamp", "turn_ids"},
            )
            self.assertEqual(directory_rows[0]["turn_ids"], [current])
            store.close()
