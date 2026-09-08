import tempfile
import unittest
from pathlib import Path

from momoi.models import TurnDraft
from momoi.runtime.transcript.building import build_groups
from momoi.runtime.transcript.rendering import render_messages
from momoi.storage import Store


class TranscriptWindowTest(unittest.TestCase):
    def test_committed_goal_bubbles_are_visible_before_delivery_and_track_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            self._add_visible_turn(store, 1)
            self.assertEqual(store.transcript_window_turn_limit(4, 8), 4)
            store.begin_turn("goal-turn", "goal", ["goal:water"])
            store.queue_progress(
                "goal-turn", "bubbles", ["两点了", "起来走走\n喝口水"], "napcat",
            )
            store.queue_progress(
                "goal-turn", "voice", ["语音提醒"], "napcat", voice=True,
            )
            store.commit_autonomous_turn("water", TurnDraft(), turn_id="goal-turn")

            def rows():
                return store.recent_conversation_messages(1, 10000)

            def rendered():
                return str(render_messages(build_groups(rows()), timezone=store.timezone))

            # The next Goal sees the complete committed speech even if delivery
            # has not started. A wholly queued Turn also advances the window.
            self.assertEqual(store.transcript_window_turn_limit(4, 8), 5)
            self.assertEqual([row["content"] for row in rows()], [
                "两点了", "起来走走\n喝口水", "语音提醒",
            ])
            self.assertEqual([row["delivery_state"] for row in rows()], ["queued"] * 3)
            self.assertEqual(rendered().count('delivery="queued"'), 3)

            first = store.due_outbox()[0]
            store.mark_sending(first.id)
            store.mark_sent(first.id)
            self.assertEqual([row["delivery_state"] for row in rows()], [
                "delivered", "queued", "queued",
            ])
            self.assertEqual(rendered().count('delivery="queued"'), 2)

            second = store.due_outbox()[0]
            store.mark_failed(second.id, "delivery failed")
            self.assertEqual([row["content"] for row in rows()], ["两点了", "语音提醒"])
            self.assertEqual(store.cancel_pending_outbox("napcat", "new owner message"), 1)
            self.assertEqual([row["content"] for row in rows()], ["两点了"])
            self.assertNotIn('delivery="queued"', rendered())
            store.close()

    @staticmethod
    def _add_visible_turn(
        store: Store,
        index: int,
        *,
        workflow_kind: str = "owner",
        source: str | None = None,
    ) -> str:
        turn_id = f"turn-{index:04d}"
        store.begin_turn(turn_id, workflow_kind, [source or turn_id])
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

    def test_manual_compaction_persists_and_resumes_growth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            for index in range(1, 5):
                self._add_visible_turn(store, index)
            self.assertEqual(store.transcript_window_turn_limit(4, 8), 4)
            for index in range(5, 7):
                self._add_visible_turn(store, index)
            self.assertEqual(store.transcript_window_turn_limit(4, 8), 6)
            # Include a completed Turn not yet observed by a context build.
            self._add_visible_turn(store, 7)
            self.assertEqual(
                store.transcript_window_turn_limit(4, 8, force_compact=True), 4
            )
            store.close()

            reopened = Store(path)
            self.assertEqual(reopened.transcript_window_turn_limit(4, 8), 4)
            self.assertEqual(
                [row["content"] for row in reopened.recent_conversation_messages(4, 10000)],
                [f"message {index}" for index in range(4, 8)],
            )
            self.assertEqual(len(reopened.recent_conversation_messages(8, 10000)), 7)
            self._add_visible_turn(reopened, 8)
            self.assertEqual(reopened.transcript_window_turn_limit(4, 8), 5)
            reopened.close()

    def test_manual_compaction_with_empty_or_fixed_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            self.assertEqual(
                store.transcript_window_turn_limit(4, 8, force_compact=True), 4
            )
            self._add_visible_turn(store, 1)
            self.assertEqual(
                store.transcript_window_turn_limit(4, 8, force_compact=True), 4
            )
            self.assertEqual(
                store.transcript_window_turn_limit(4, 4, force_compact=True), 4
            )
            store.close()

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
                workflow_kind="heartbeat",
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
