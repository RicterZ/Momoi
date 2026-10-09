from tests.support import provider_catalog
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage
from momoi.runtime import MomoiDaemon
from datetime import datetime, timezone
from xml.etree import ElementTree

import pytest

from momoi.storage import Store
from momoi.storage.core.migrations import MIGRATIONS, _add_turn_parent
from momoi.runtime.turn_support import pack_user_context


def test_old_database_upgrades_before_parent_index(tmp_path):
    path = tmp_path / "momoi.sqlite3"
    store = Store(path)
    store.begin_turn("owner", "owner", [])
    store._db.execute("DROP INDEX turns_parent")
    store._db.execute("ALTER TABLE turns DROP COLUMN parent_turn_id")
    store._db.execute(f"PRAGMA user_version={MIGRATIONS.index(_add_turn_parent)}")
    store._db.commit()
    store.close()
    store = Store(path)
    try:
        store.begin_turn("follow", "reply_followup", [], parent_turn_id="owner")
        assert store._db.execute("SELECT parent_turn_id FROM turns WHERE id='follow'").fetchone()[0] == "owner"
        with pytest.raises(ValueError, match="parent cannot change"):
            store.begin_turn("follow", "reply_followup", [], parent_turn_id="different")
        with pytest.raises(ValueError, match="own parent"):
            store.begin_turn("self", "owner", [], parent_turn_id="self")
    finally:
        store.close()


def test_followup_merges_across_months_without_merging_other_children(tmp_path):
    store = Store(tmp_path / "momoi.sqlite3")
    try:
        store.begin_turn("owner", "owner", [])
        store.begin_turn("follow", "reply_followup", [], parent_turn_id="owner")
        store.begin_turn("maintenance", "current_state_maintenance", [], parent_turn_id="owner")
        for tid, stage, month in [
            ("owner", "owner", 8), ("follow", "reply_followup", 9),
            ("maintenance", "current_state_maintenance", 9),
        ]:
            store.record_thinking_call(
                turn_id=tid, call_id=tid, stage=stage, reasoning=tid,
                created_at=datetime(2026, month, 15, tzinfo=timezone.utc).timestamp(),
            )
        page = store.dashboard_thinking(month="2026-09")
        items = {item["id"]: item for item in page["items"]}
        assert set(items) == {"owner", "maintenance"}
        assert items["owner"]["excerpt"] == "owner"
        assert items["owner"]["call_count"] == 2
        assert items["owner"]["turn_ids"] == ["owner", "follow"]
        detail = store.dashboard_thinking_detail("follow")
        assert detail["turn_id"] == "owner"
        assert [call["turn_id"] for call in detail["calls"]] == ["owner", "follow"]
        assert len(store.read_thinking("follow")["calls"]) == 1
    finally:
        store.close()


def test_followup_xml_is_not_wrapped_or_escaped():
    text = pack_user_context(("followup",
        '<followup parent_turn_id="owner" silent_minutes="6"><reason>A &amp; B</reason></followup>'))
    root = ElementTree.fromstring(text)
    assert root.attrib == {"parent_turn_id": "owner", "silent_minutes": "6"}
    assert root.find("reason").text == "A & B"
    assert root.find("followup") is None


@pytest.mark.parametrize("reasonings, expected", [
    (["", "Owner reasoning", ""], "Owner reasoning"),
    (["x" * 500, "follow"], "x" * 400),
    (["", ""], ""),
])
def test_merged_excerpt_uses_first_visible_reasoning(tmp_path, reasonings, expected):
    store = Store(tmp_path / "momoi.sqlite3")
    try:
        store.begin_turn("owner", "owner", [])
        store.begin_turn("follow", "reply_followup", [], parent_turn_id="owner")
        for index, reasoning in enumerate(reasonings):
            store.record_thinking_call(
                turn_id="owner" if index == 0 else "follow",
                call_id=str(index), stage="owner" if index == 0 else "reply_followup",
                created_at=100 + index, reasoning=reasoning,
            )
        item = store.dashboard_thinking(month="all")["items"][0]
        assert item["id"] == "owner"
        assert item["excerpt"] == expected
    finally:
        store.close()


class ReplyWaitNativeTranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_followup_continues_after_native_shared_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_unsummarized_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            event = IncomingMessage("reply:event", "1", "晚上选个游戏吧", 1, 1)
            daemon.store.add_event(event)
            owner_turn = daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(["那你想玩解谜还是动作呀"]),
            )
            outbox_id = daemon.store._db.execute(
                "SELECT id FROM outbox WHERE turn_id=?", (owner_turn,)
            ).fetchone()["id"]
            daemon.store.mark_sent(int(outbox_id))
            daemon.store._db.execute(
                """UPDATE self_state
                   SET pending_reply_turn_id=?,
                       pending_reply_expectation='主人对问题的回答',
                       pending_reply_since=1000,
                       pending_reply_last_reason='这个问题需要老师决定',
                       pending_reply_delay_minutes=4,
                       pending_reply_next_check_at=1240
                   WHERE id=1""",
                (owner_turn,),
            )
            daemon.store._db.commit()

            # Freeze the shared prefix before a dashboard edit during reply wait.
            from tests.test_transcript_memory import add
            memory_id = add(daemon.store, "等待期间的旧偏好")
            baseline = daemon.shared_turn_context("reply-followup")["messages"][0]["content"]
            daemon.store.update_memory_content(memory_id, "等待期间的新偏好")
            terminal = AgentReply([], reply_wait={"wait": False})
            with (
                patch.object(
                    daemon,
                    "_run_tool_loop",
                    new_callable=AsyncMock,
                    return_value=terminal,
                ) as run,
                patch.object(daemon.store, "commit_reply_followup"),
            ):
                await daemon._complete_reply_wait(
                    "reply-followup", "napcat", owner_event_revision=1
                )

            system = str(run.await_args.args[0])
            messages = run.await_args.args[1]
            tools = run.await_args.args[2]
            rendered = json.dumps(messages, ensure_ascii=False)
            self.assertEqual(messages[0]["content"], baseline)
            self.assertIn("等待期间的新偏好", rendered)
            self.assertIn("<replace", rendered)
            self.assertTrue(messages[2]["_memory_change"])
            draft = run.await_args.args[4]
            self.assertEqual(draft.memory_context[memory_id]["content"], "等待期间的新偏好")
            self.assertNotIn("Required reply follow-up", system)
            self.assertIn("<workflow_contract>", rendered)
            self.assertNotIn("<reply_timeline>", rendered)
            from xml.etree import ElementTree
            current = messages[-1]["content"][0]["text"]
            node = ElementTree.fromstring("<root>" + current + "</root>").find("followup")
            self.assertEqual(node.attrib["parent_turn_id"], owner_turn)
            self.assertGreaterEqual(int(node.attrib["silent_minutes"]), 0)
            self.assertEqual(node.find("reason").text, "这个问题需要老师决定")
            self.assertIsNone(node.find("followup"))
            self.assertIn("<recent_episodes>", str(messages[1]["content"]))
            self.assertEqual(
                [message["role"] for message in messages],
                ["user", "user", "user", "user"],
            )
            self.assertNotIn("[runtime time gap]", str(messages[-1]["content"]))
            self.assertNotIn("晚上选个游戏吧", rendered)
            self.assertNotIn("那你想玩解谜还是动作呀", rendered)
            self.assertEqual(
                tools,
                daemon.tool_surface.conversation_specs(),
            )
            daemon.store.close()
