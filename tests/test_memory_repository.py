import sqlite3
import time
from pathlib import Path

import pytest

from momoi.memory.storage.repository import MemoryRepository
from momoi.memory.storage.transactions import transaction


@pytest.fixture
def database(tmp_path):
    db = sqlite3.connect(tmp_path / "memory.sqlite3")
    db.row_factory = sqlite3.Row
    # Use the actual schema, but only the three tables owned by this repository.
    # No events, turns, goals, journal, or application bootstrap are available.
    schema = Path(__file__).parents[1] / "src/momoi/storage/core/schema.sql"
    text = schema.read_text()
    for table in ("memories", "memory_evidence", "memory_tombstones"):
        start = text.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        end = text.index(";", start) + 1
        db.execute(text[start:end])
    db.execute("CREATE TABLE application_work (id TEXT PRIMARY KEY)")
    yield db
    db.close()


def write(repo, *, key="drink", text="喜欢无糖咖啡", targets=(), evidence=None):
    source = {"event_id": "event:1", "quote": text}
    return repo.write(
        {"kind": "preference", "key": key, "content": text,
         "activation": "recall", "expires_at": None},
        list(targets), source, [source] if evidence is None else evidence,
        now=time.time(),
    )


def test_repository_write_and_evidence_join_caller_transaction(database):
    repo = MemoryRepository(database)
    with pytest.raises(RuntimeError, match="abort"):
        with transaction(database):
            database.execute("INSERT INTO application_work VALUES ('outer')")
            memory_id = write(repo)
            assert repo.active("preference", "drink")["id"] == memory_id
            assert database.in_transaction
            raise RuntimeError("abort")
    for table in ("memories", "memory_evidence", "application_work"):
        assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not database.in_transaction


def test_failed_readdition_restores_tombstone_without_rolling_back_caller(database):
    repo = MemoryRepository(database)
    original_id = write(repo)
    repo.forget(repo.snapshots([original_id])[original_id],
                {"event_id": "forget", "quote": "忘记"}, now=time.time())
    with transaction(database):
        database.execute("INSERT INTO application_work VALUES ('outer')")
        # Fails after inserting the replacement and removing the tombstone.
        with pytest.raises(KeyError):
            write(repo, evidence=[{"event_id": "missing-quote"}])
        assert database.in_transaction
        assert not repo.has("preference", "drink")
        assert database.execute("SELECT superseded_by FROM memories WHERE id=?",
                                (original_id,)).fetchone()[0] is None
        assert database.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM memory_tombstones").fetchone()[0] == 1
    assert database.execute("SELECT id FROM application_work").fetchone()[0] == "outer"


def test_standalone_commits_and_snapshot_conflicts(database):
    repo = MemoryRepository(database)
    memory_id = write(repo)
    assert not database.in_transaction
    snapshots = repo.snapshots([memory_id])
    assert repo.validate_snapshots(snapshots) == snapshots
    with pytest.raises(ValueError, match="memory_key_conflict"):
        write(repo, text="喜欢茶")
    assert repo.validate_snapshots(snapshots) == snapshots
    replacement = write(repo, text="喜欢茶", targets=[memory_id])
    with pytest.raises(ValueError, match="memory_snapshot_changed"):
        repo.validate_snapshots(snapshots)
    assert repo.active("preference", "drink")["id"] == replacement
    assert {row[0] for row in database.execute(
        "SELECT quote FROM memory_evidence WHERE memory_id=?", (replacement,),
    )} == {"喜欢无糖咖啡", "喜欢茶"}


def test_merge_keeps_evidence_and_rolls_back_with_outer_transaction(database):
    repo = MemoryRepository(database)
    survivor = write(repo)
    other = write(repo, key="other", text="咖啡不加糖")
    snapshots = repo.snapshots([survivor, other])
    events = [{"id": "new", "content": "无糖咖啡", "occurred_at": time.time()}]
    with pytest.raises(RuntimeError):
        with transaction(database):
            repo.merge(survivor, [other], "无糖咖啡", "recall", None, events)
            assert not repo.snapshots([other])
            raise RuntimeError("abort")
    assert repo.validate_snapshots(snapshots) == snapshots
    repo.merge(survivor, [other], "无糖咖啡", "recall", None, events)
    assert not database.in_transaction
    assert set(repo.snapshots([survivor, other])) == {survivor}
    assert {row[0] for row in database.execute(
        "SELECT quote FROM memory_evidence WHERE memory_id=?", (survivor,),
    )} == {"喜欢无糖咖啡", "咖啡不加糖", "无糖咖啡"}
