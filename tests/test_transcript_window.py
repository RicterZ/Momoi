import tempfile
import unittest
from pathlib import Path

from momoi.models import TurnDraft, IncomingMessage, AgentReply
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.channel.napcat import NapCatConfig
from momoi.runtime import MomoiDaemon
from tests.support import provider_catalog
import json
from momoi.runtime.transcript.building import build_groups
from momoi.runtime.transcript.rendering import render_messages
from momoi.storage import Store
from momoi.storage.core.migrations import MIGRATIONS, _add_transcript_window_observed_total


class TranscriptWindowTest(unittest.TestCase):
    def test_native_replay_includes_followup_archived_under_owner_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store.sqlite3")
            event = IncomingMessage("request", "1", "一分钟后跟进", 1, 1)
            store.add_event(event)
            store.commit_turn([event], event.text, AgentReply(["计时开始"]), turn_id="owner")
            store.begin_turn("followup", "reply_followup", ["owner"])
            store.queue_progress("followup", "send-followup", ["一分钟到，我来了"], "napcat")
            outbox = store._db.execute("SELECT id FROM outbox WHERE turn_id='followup'").fetchone()[0]
            with store._db:
                store._db.execute(
                    "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json,outbox_id,delivery_state) VALUES ('owner','assistant','一分钟到，我来了',2,'[]',?,'delivered')",
                    (outbox,),
                )
                store._db.execute("UPDATE turns SET state='completed' WHERE id='followup'")
            for turn, identifier, text in (("owner", "send-owner", "计时开始"), ("followup", "send-followup", "一分钟到，我来了")):
                store.append_turn_journal(turn, "assistant_exchange", {
                    "content": [{"type": "tool_use", "id": identifier, "name": "send_bubbles", "input": {"bubbles": [text]}}],
                    "results": [{"type": "tool_result", "tool_use_id": identifier, "content": '{"ok":true}'}],
                }, trust="runtime")
            exchanges = store.turn_exchanges(["owner"])
            self.assertEqual(len(exchanges["owner"]), 2)
            rows = store.conversation_messages_for_turns(["owner"])
            rendered = render_messages(build_groups(rows), timezone=store.timezone, native_exchanges=exchanges)
            text = json.dumps(rendered, ensure_ascii=False)
            self.assertIn("一分钟到，我来了", text)
            self.assertLess(text.index("计时开始"), text.index("一分钟到，我来了"))
            store.close()

    def test_shared_prefix_is_identical_for_each_stage_at_same_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(AppConfig(
                providers=provider_catalog(LLMConfig("http://localhost", "test", "model", 100, 0, 1, 0)),
                channel=NapCatConfig("ws://localhost", "123", 1, 60, 30, 30, 20),
                system_prompt="test", transcript_turns_min=4, transcript_turns_max=4,
                episode_unsummarized_tail_turns=2, memory_results=2,
                database=Path(directory) / "store.sqlite3", log_level="INFO",
            ))
            event = IncomingMessage("past", "1", "过去的问题", 1, 1)
            daemon.store.add_event(event)
            daemon.store.commit_turn([event], event.text, AgentReply(["过去的回答"]), turn_id="past")
            daemon.store.append_turn_journal("past", "assistant_exchange", {
                "content": [{"type": "tool_use", "id": "send-past", "name": "send_bubbles", "input": {"bubbles": ["过去的回答"]}}],
                "results": [{"type": "tool_result", "tool_use_id": "send-past", "content": '{"ok":true}'}],
            }, trust="runtime")
            for stage in ("owner", "heartbeat", "goal", "webhook", "reply_followup", "plan_step"):
                daemon.store.begin_turn(f"active-{stage}", stage, [stage])
            with daemon.store._db:
                daemon.store._db.execute(
                    "UPDATE turns SET started_at=100,updated_at=100 WHERE id LIKE 'active-%'"
                )
                daemon.store._db.execute("UPDATE turns SET updated_at=10 WHERE id='past'")
            contexts = [daemon.shared_turn_context(f"active-{stage}")["messages"]
                        for stage in ("owner", "heartbeat", "goal", "webhook", "reply_followup", "plan_step")]
            encoded = [json.dumps(messages, ensure_ascii=False, sort_keys=True) for messages in contexts]
            self.assertEqual(len(set(encoded)), 1)
            self.assertIn("过去的问题", encoded[0])
            self.assertIn("过去的回答", encoded[0])
            daemon.store.close()

    def test_shared_history_starts_after_last_legacy_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(AppConfig(
                providers=provider_catalog(LLMConfig("http://localhost", "test", "model", 100, 0, 1, 0)),
                channel=NapCatConfig("ws://localhost", "123", 1, 60, 30, 30, 20),
                system_prompt="test", transcript_turns_min=8, transcript_turns_max=8,
                episode_unsummarized_tail_turns=2, memory_results=2,
                database=Path(directory) / "store.sqlite3", log_level="INFO",
            ))
            for index in range(1, 5):
                turn_id = f"turn-{index}"
                event = IncomingMessage(turn_id, "1", f"问题{index}", index, index)
                daemon.store.add_event(event)
                daemon.store.commit_turn([event], event.text, AgentReply([f"回答{index}"]), turn_id=turn_id)
                if index in (2, 4):
                    daemon.store.append_turn_journal(turn_id, "assistant_exchange", {
                        "content": [{"type": "tool_use", "id": f"send-{index}", "name": "send_bubbles", "input": {"bubbles": [f"回答{index}"]}}],
                        "results": [{"type": "tool_result", "tool_use_id": f"send-{index}", "content": '{"ok":true}'}],
                    }, trust="runtime")
                with daemon.store._db:
                    daemon.store._db.execute("UPDATE turns SET updated_at=? WHERE id=?", (index, turn_id))
            daemon.store.begin_turn("active", "owner", ["active"])
            daemon.store.create_episode("旧话题", episode_id="legacy-topic")
            daemon.store.link_turn_to_episode("legacy-topic", "turn-1")
            with daemon.store._db:
                daemon.store._db.execute(
                    "UPDATE conversation_episodes SET narrative_summary='旧话题的压缩摘要' WHERE id='legacy-topic'"
                )
            shared = daemon.shared_turn_context("active")
            self.assertEqual({row["turn_id"] for row in shared["rows"]}, {"turn-4"})
            text = json.dumps(shared["history"], ensure_ascii=False)
            self.assertIn("问题4", text)
            self.assertIn("回答4", text)
            self.assertNotIn("问题3", text)
            self.assertNotIn("回答2", text)
            self.assertNotIn("historical_recall", text)
            self.assertIn("旧话题的压缩摘要", json.dumps(shared["messages"][1], ensure_ascii=False))
            # A wholly legacy window has no detailed transcript.
            with daemon.store._db:
                daemon.store._db.execute("DELETE FROM turn_journal WHERE item_type='assistant_exchange'")
            self.assertEqual(daemon.shared_turn_context("active")["history"], [])
            daemon.store.close()

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

    def test_small_budget_does_not_truncate_latest_turn_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            self._add_visible_turn(store, 1)
            expected = store.recent_conversation_messages(1, 10000)
            actual = store.recent_conversation_messages(1, 1)
            self.assertEqual(actual, expected)
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

    def test_updating_completed_turn_does_not_prepend_old_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            for index in range(1, 8):
                self._add_visible_turn(store, index)
            self.assertEqual(store.transcript_window_turn_limit(4, 8), 4)
            initial = store.recent_conversation_messages(4, 10000)
            with store._db:
                store._db.execute(
                    "UPDATE turns SET updated_at=7.5 WHERE id='turn-0007'"
                )
            self.assertEqual(store.transcript_window_turn_limit(4, 8), 4)
            self.assertEqual(store.recent_conversation_messages(4, 10000), initial)

            self._add_visible_turn(store, 8)
            self.assertEqual(store.transcript_window_turn_limit(4, 8), 5)
            extended = store.recent_conversation_messages(5, 10000)
            self.assertEqual(extended[:len(initial)], initial)
            store.close()

    def test_existing_window_state_migrates_without_growth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            store = Store(path)
            for index in range(1, 8):
                self._add_visible_turn(store, index)
            with store._db:
                store._db.execute("DROP TABLE transcript_window_state")
                store._db.execute(
                    """CREATE TABLE transcript_window_state (
                       id INTEGER PRIMARY KEY CHECK (id = 1),
                       current_turns INTEGER NOT NULL,
                       observed_turn_id TEXT NOT NULL,
                       observed_updated_at REAL NOT NULL)"""
                )
                store._db.execute(
                    "INSERT INTO transcript_window_state VALUES (1, 5, 'turn-0007', 7)"
                )
                store._db.execute(
                    f"PRAGMA user_version={MIGRATIONS.index(_add_transcript_window_observed_total)}"
                )
            store.close()

            reopened = Store(path)
            self.assertEqual(reopened.transcript_window_turn_limit(4, 8), 5)
            state = reopened._db.execute(
                "SELECT observed_total_turns FROM transcript_window_state WHERE id=1"
            ).fetchone()
            self.assertEqual(state[0], 7)
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
                {"id", "title", "narrative_summary",
                 "last_activity_timestamp", "turn_ids"},
            )
            self.assertEqual(directory_rows[0]["turn_ids"], [current])
            store.close()

    def test_review_window_replays_only_wholly_contained_completed_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'store.sqlite3')
            for turn, start, end in [('inside', 110, 190), ('left', 90, 150), ('right', 150, 200)]:
                store.begin_turn(turn, 'owner', [turn])
                store.append_turn_journal(turn, 'assistant_exchange', {
                    'content': [{'type': 'tool_use', 'id': turn, 'name': 'read_file', 'input': {'path': turn}}],
                    'results': [{'type': 'tool_result', 'tool_use_id': turn, 'content': '{"ok":true}'}],
                }, trust='runtime')
                with store._db:
                    store._db.execute("UPDATE turns SET state='completed', started_at=?, updated_at=? WHERE id=?", (start, end, turn))
                    store._db.execute('UPDATE turn_journal SET created_at=? WHERE turn_id=?', (end-1, turn))
            self.assertEqual(set(store.turn_exchanges(['inside', 'left', 'right'], window=(100, 200))), {'inside'})
            self.assertEqual(set(store.turn_exchanges(['inside', 'left', 'right'])), {'inside', 'left', 'right'})
