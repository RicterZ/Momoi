import json
import sqlite3
import time

import pytest

from momoi.storage import Store
from momoi.storage.core.migrations import _add_webhook_video
from momoi.storage.core import migrations
from momoi.storage.core.schema import initialize_schema
from momoi.dashboard.backup import schema as backup_schema


def create_run(store, mode="file"):
    run, _ = store.create_webhook_run("camera-event", None, {"steps": [
        {"id": "notify", "uses": "message", "prompt": "camera"},
        {"id": "clip", "uses": "video", "source": "https://camera.test/live",
         "send_as": mode, "duration_seconds": 30},
    ]})
    run_id = run["id"]
    path = store.webhook_media_directory / f"{run_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"recorded video")
    return run_id, {"path": str(path), "duration_seconds": 30, "size_bytes": path.stat().st_size}


@pytest.mark.parametrize("mode", ["file", "video"])
@pytest.mark.parametrize("message_state", ["succeeded", "failed", "ambiguous"])
def test_video_enqueue_waits_for_notification_and_is_idempotent(tmp_path, mode, message_state):
    store = Store(tmp_path / "state.db")
    try:
        run_id, asset = create_run(store, mode)
        assert store.begin_webhook_video(run_id, 1)
        assert not store.begin_webhook_video(run_id, 1)
        assert store.record_webhook_video(run_id, 1, asset)
        assert not store.record_webhook_video(run_id, 1, asset)
        assert store.enqueue_webhook_video(run_id, 1, "onebot") == []
        store.start_webhook_step(run_id, 0)
        store.finish_webhook_step(run_id, 0, message_state, {}, "oops" if message_state != "succeeded" else None)
        assert store.webhook_run(run_id)["state"] not in {"succeeded", "failed", "ambiguous"}
        ids = store.enqueue_webhook_video(run_id, 1, "onebot")
        assert len(ids) == 1
        assert store.enqueue_webhook_video(run_id, 1, "onebot") == ids
        row = store._db.execute("SELECT * FROM outbox WHERE id=?", ids).fetchone()
        segment = json.loads(row["payload_json"])["segments"][0]
        assert segment["type"] == mode
        assert segment["send_as"] == mode
        assert segment["data"]["file"] == asset["path"]
        assert store.claim_webhook_run() is None
        assert store._db.execute("SELECT count(*) FROM messages WHERE outbox_id=?", ids).fetchone()[0] == 1
        store._db.execute("UPDATE outbox SET state='sent' WHERE id=?", ids)
        store._db.commit()
        assert store.webhook_delivery_state(run_id, 1) == "succeeded"
        store.finish_webhook_step(run_id, 1, "succeeded", {}, None)
        assert store.webhook_run(run_id)["state"] == message_state
        assert store.webhook_step(run_id, 1)["result"]["path"] == asset["path"]
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["queued", "recording", "recorded", "enqueued"])
def test_recovery_does_not_rerecord_live_footage(tmp_path, stage):
    db = tmp_path / "state.db"
    store = Store(db)
    run_id, asset = create_run(store)
    part = store.webhook_media_directory / f"{run_id}.part.mp4"
    if stage != "queued":
        part.write_bytes(b"partial")
        store.begin_webhook_video(run_id, 1, str(part))
    if stage in {"recorded", "enqueued"}:
        store.record_webhook_video(run_id, 1, asset)
    if stage == "enqueued":
        store.finish_webhook_step(run_id, 0, "succeeded", {}, None)
        ids = store.enqueue_webhook_video(run_id, 1)
    store.close()
    store = Store(db)
    try:
        video = store.webhook_step(run_id, 1)
        if stage in {"queued", "recording"}:
            assert video["state"] == "failed"
            assert video["error"] == "recording_interrupted"
            assert not part.exists()
        elif stage == "recorded":
            assert video["result"]["status"] == "recorded"
            store.finish_webhook_step(run_id, 0, "succeeded", {}, None)
            assert store.enqueue_webhook_video(run_id, 1)
        else:
            assert video["result"]["status"] == "enqueued"
            assert store.enqueue_webhook_video(run_id, 1) == ids
        if stage in {"recorded", "enqueued"}:
            assert store.recoverable_webhook_video_runs()[0]["plan"]["steps"][1]["uses"] == "video"
    finally:
        store.close()


def test_ambiguous_delivery_is_terminal_and_assets_retained(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        run_id, asset = create_run(store)
        store.begin_webhook_video(run_id, 1)
        store.record_webhook_video(run_id, 1, asset)
        store.finish_webhook_step(run_id, 0, "succeeded", {}, None)
        ids = store.enqueue_webhook_video(run_id, 1)
        store._db.execute("UPDATE outbox SET state='ambiguous' WHERE id=?", ids)
        store._db.commit()
        assert store.webhook_delivery_state(run_id, 1) == "ambiguous"
        store.finish_webhook_step(run_id, 1, "ambiguous", {}, "unknown")
        assert store.webhook_run(run_id)["state"] == "ambiguous"
        store._db.execute("UPDATE webhook_steps SET completed_at=? WHERE run_id=? AND kind='video'", (time.time() - 2 * 86400, run_id))
        store._db.commit()
        assert store.cleanup_webhook_video_assets(store.webhook_media_directory) == 0
        assert store.cleanup_webhook_video_assets(store.webhook_media_directory, failed_days=1) == 1
        assert not (store.webhook_media_directory / f"{run_id}.mp4").exists()
    finally:
        store.close()


def test_recording_failure_preserves_notification_work(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        run_id, _ = create_run(store)
        store.begin_webhook_video(run_id, 1)
        store.finish_webhook_step(run_id, 1, "failed", {}, "connection_failed")
        claimed = store.claim_webhook_run()
        assert claimed["id"] == run_id
        assert claimed["current_step"] == 0
        store.finish_webhook_step(run_id, 0, "succeeded", {}, None)
        assert store.webhook_run(run_id)["state"] == "failed"
    finally:
        store.close()


def test_enqueue_rolls_back_outbox_if_step_update_fails(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        run_id, asset = create_run(store)
        store.begin_webhook_video(run_id, 1)
        store.record_webhook_video(run_id, 1, asset)
        store.finish_webhook_step(run_id, 0, "succeeded", {}, None)
        store._db.execute("""CREATE TRIGGER reject_video_enqueue
            BEFORE UPDATE ON webhook_steps WHEN NEW.state='waiting_delivery'
            BEGIN SELECT RAISE(ABORT, 'simulate write failure'); END""")
        store._db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="simulate write failure"):
            store.enqueue_webhook_video(run_id, 1)
        assert store._db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert store.webhook_step(run_id, 1)["result"]["status"] == "recorded"
    finally:
        store.close()


def test_restart_sending_video_stays_ambiguous_even_after_multiple_attempts(tmp_path):
    database = tmp_path / "state.db"
    store = Store(database)
    run_id, asset = create_run(store)
    store.begin_webhook_video(run_id, 1)
    store.record_webhook_video(run_id, 1, asset)
    store.finish_webhook_step(run_id, 0, "succeeded", {}, None)
    ids = store.enqueue_webhook_video(run_id, 1)
    store._db.execute("UPDATE outbox SET state='sending', attempts=3 WHERE id=?", ids)
    store._db.commit()
    store.close()
    store = Store(database)
    try:
        assert store.webhook_delivery_state(run_id, 1) == "ambiguous"
        assert store._db.execute("SELECT state FROM outbox WHERE id=?", ids).fetchone()[0] == "ambiguous"
    finally:
        store.close()


def test_successful_video_expires_after_one_day(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        run_id, asset = create_run(store)
        store.begin_webhook_video(run_id, 1)
        store.record_webhook_video(run_id, 1, asset)
        store.finish_webhook_step(run_id, 0, "succeeded", {}, None)
        store.enqueue_webhook_video(run_id, 1)
        store.finish_webhook_step(run_id, 1, "succeeded", {}, None)
        assert store.cleanup_webhook_video_assets(store.webhook_media_directory) == 0
        store._db.execute("UPDATE webhook_steps SET completed_at=? WHERE run_id=? AND kind='video'", (time.time() - 2 * 86400, run_id))
        store._db.commit()
        assert store.cleanup_webhook_video_assets(store.webhook_media_directory) == 1
        assert store.cleanup_webhook_video_assets(store.webhook_media_directory) == 0
    finally:
        store.close()


def test_video_migration_preserves_steps_and_foreign_keys():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE webhook_runs(id TEXT PRIMARY KEY);
        INSERT INTO webhook_runs VALUES ('old');
        CREATE TABLE webhook_steps (
            run_id TEXT REFERENCES webhook_runs(id), step_index INTEGER,
            kind TEXT CHECK(kind IN ('message', 'exec')),
            PRIMARY KEY(run_id, step_index));
        INSERT INTO webhook_steps VALUES ('old', 0, 'message');
    """)
    _add_webhook_video(db)
    assert db.execute("SELECT * FROM webhook_steps").fetchall() == [("old", 0, "message")]
    db.execute("INSERT INTO webhook_steps VALUES ('old', 1, 'video')")
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_video_migration_schema_matches_fresh_database_for_backups():
    with sqlite3.connect(":memory:") as upgraded, sqlite3.connect(":memory:") as fresh:
        initialize_schema(upgraded)
        historical = upgraded.execute(
            "SELECT sql FROM sqlite_master WHERE name='webhook_steps'"
        ).fetchone()[0].replace('"webhook_steps"', 'webhook_steps').replace(
            "('message', 'exec', 'video')", "('message', 'exec')"
        )
        upgraded.execute("DROP TABLE webhook_steps")
        upgraded.execute(historical)
        upgraded.execute(f"PRAGMA user_version={migrations.MIGRATIONS.index(_add_webhook_video)}")
        upgraded.commit()
        initialize_schema(upgraded)
        initialize_schema(fresh)
        assert backup_schema(upgraded) == backup_schema(fresh)


@pytest.mark.parametrize("exec_state", ["failed", "ambiguous"])
@pytest.mark.parametrize("restart", [False, True])
def test_exec_failure_stops_later_notification_but_video_remains_independent(
    tmp_path, exec_state, restart
):
    database = tmp_path / "state.db"
    store = Store(database)
    run, _ = store.create_webhook_run("camera-exec", None, {"steps": [
        {"id": "check", "uses": "exec", "argv": ["false"], "env": {}},
        {"id": "notify", "uses": "message", "prompt": "camera"},
        {"id": "clip", "uses": "video", "source": "https://camera.test/live",
         "send_as": "file", "duration_seconds": 30},
    ]})
    run_id = run["id"]
    store.begin_webhook_video(run_id, 2)
    store.record_webhook_video(run_id, 2, {
        "path": str(store.webhook_media_directory / f"{run_id}.mp4"),
        "duration_seconds": 30, "size_bytes": 42,
    })
    store.start_webhook_step(run_id, 0)
    if not restart or exec_state == "failed":
        store.finish_webhook_step(run_id, 0, exec_state, {}, "executor_error")
    if restart:
        store.close()
        store = Store(database)
    try:
        assert store.webhook_step(run_id, 1)["state"] == "failed"
        assert store.webhook_step(run_id, 1)["error"] == "previous_step_failed"
        assert store.claim_webhook_run() is None
        assert store.webhook_run(run_id)["state"] not in {"failed", "ambiguous"}
        assert store.enqueue_webhook_video(run_id, 2)
        store.finish_webhook_step(run_id, 2, "succeeded", {}, None)
        assert store.webhook_run(run_id)["state"] == exec_state
    finally:
        store.close()
