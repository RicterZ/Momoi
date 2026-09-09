import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from momoi.storage import Store
from momoi.storage.thinking import ThinkingStore


spec = importlib.util.spec_from_file_location(
    "clear_chat_after", Path(__file__).parents[1] / "scripts/clear_chat_after.py",
)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path
    (root / "data").mkdir()
    (root / "config.json").write_text(json.dumps({"storage": {"database": "data/momoi.sqlite3", "thinking": "data/thinking"}}))
    store = Store(root / "data/momoi.sqlite3")
    store.create_episode("older topic", episode_id="mixed")
    store.create_episode("later topic", episode_id="later-only")
    with store._db:
        for identifier, at in [("before", 10), ("anchor", 20), ("later", 30)]:
            store._db.execute("INSERT INTO turns(id,kind,source_ids_json,state,started_at,updated_at) VALUES (?,'owner','[]','completed',?,?)", (identifier, at, at + 5))
        store._db.execute("INSERT INTO events(id,message_id,kind,content,occurred_at,received_at,processed) VALUES ('later-event','later-event','test','PRIVATE LATER',30,30,1)")
        for identifier, turn, at in [(1, "before", 10), (2, "anchor", 25), (3, "anchor", 25), (4, "later", 35)]:
            store._db.execute("INSERT INTO outbox(id,turn_id,dedupe_key,text,state,next_attempt_at) VALUES (?,?,?,'PRIVATE OUTBOX','sent',0)", (identifier, turn, str(identifier)))
            store._db.execute("INSERT INTO messages(id,turn_id,role,content,created_at,source_event_ids_json,outbox_id) VALUES (?,?,'assistant',?,?,?,?)",
                              (identifier, turn, 'PRIVATE MESSAGE ' + str(identifier), at, '["later-event"]' if turn == "later" else '[]', identifier))
        store._db.execute("UPDATE self_state SET mood_state='new',mood_intensity=0.9,mood_cause='PRIVATE LATER',mood_updated_at=40,pending_reply_turn_id='later',pending_reply_expectation='PRIVATE WAIT'")
    store.append_turn_journal("before", "final", {"mood_change": {"state": "calm", "intensity": 0.4, "cause": "PRIVATE EARLIER"}}, created_at=15)
    store.append_turn_journal("anchor", "final", {"mood_change": {"state": "new", "intensity": 0.5, "cause": "PRIVATE ANCHOR END"}}, created_at=26)
    store.append_turn_journal("later", "final", {"mood_change": {"state": "new", "intensity": 0.9, "cause": "PRIVATE LATER"}}, created_at=40)
    store.link_turn_to_episode("mixed", "before")
    store.link_turn_to_episode("mixed", "anchor")
    store.link_turn_to_episode("mixed", "later")
    store.link_turn_to_episode("later-only", "later")
    with store._db:
        store._db.execute("UPDATE conversation_episodes SET narrative_summary='PRIVATE LATER SUMMARY'")
    store.close()
    thoughts = ThinkingStore(root / "data/thinking", ZoneInfo("UTC"))
    for at, turn in [(24, "anchor"), (26, "anchor"), (35, "later")]:
        thoughts.record(created_at=at, turn_id=turn, call_id=str(at), stage="owner", round=1, model="test", tools=[], reasoning="PRIVATE THINKING")
    thoughts.close()
    for folder in [root / "llm-dumps", root / "data/tool-results"]:
        folder.mkdir()
        for at in (24, 26, 35):
            path = folder / f"{at}.json"
            path.write_text("PRIVATE FILE")
            os.utime(path, (at, at))
    return root


def snapshot(root):
    with sqlite3.connect(root / "data/momoi.sqlite3") as db:
        return "\n".join(db.iterdump())


def test_preview_rolls_back_and_report_contains_no_content(workspace):
    before = snapshot(workspace)
    report = cleanup.clear_after(workspace, 2)
    assert report["counts"]["messages"] == 2
    assert report["applied"] is False
    assert "PRIVATE" not in json.dumps(report)
    assert snapshot(workspace) == before
    assert len(list((workspace / "llm-dumps").glob("*.json"))) == 3


def test_apply_retains_anchor_restores_mood_and_can_be_repeated(workspace):
    report = cleanup.clear_after(workspace, 2, apply=True)
    assert report["counts"]["messages"] == 2
    assert report["counts"]["outbox"] == 2
    assert report["counts"]["think0.calls"] == 2
    with sqlite3.connect(workspace / "data/momoi.sqlite3") as db:
        assert db.execute("SELECT id FROM messages ORDER BY id").fetchall() == [(1,), (2,)]
        assert db.execute("SELECT id FROM outbox ORDER BY id").fetchall() == [(1,), (2,)]
        assert db.execute("SELECT id,narrative_summary FROM conversation_episodes").fetchall() == [("mixed", "")]
        assert db.execute("SELECT mood_state,mood_cause,mood_updated_at,pending_reply_expectation FROM self_state").fetchone() == ("calm", "PRIVATE EARLIER", 15, "")
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    for folder in (workspace / "llm-dumps", workspace / "data/tool-results"):
        assert [p.name for p in folder.glob("*.json")] == ["24.json"]
    again = cleanup.clear_after(workspace, 2, apply=True)
    assert again["counts"]["messages"] == 0
    assert again["counts"]["think0.calls"] == 0
    assert again["mood_source_at"] == 15


def test_bad_anchor_leaves_database_unchanged(workspace):
    before = snapshot(workspace)
    with pytest.raises(ValueError, match="anchor_missing"):
        cleanup.clear_after(workspace, 999, apply=True)
    assert snapshot(workspace) == before


def test_apply_requires_matching_container(workspace, monkeypatch):
    before = snapshot(workspace)
    monkeypatch.setattr("sys.argv", ["clear", "--workspace", str(workspace), "--anchor-id", "2", "--apply"])
    with pytest.raises(ValueError, match="apply_requires_container"):
        cleanup.main()
    assert snapshot(workspace) == before
