import pytest
from momoi.storage import Store
from momoi.models import AgentReply, IncomingMessage


def test_repository_writes_roll_back_with_outer_transaction(tmp_path):
    store = Store(tmp_path / "db")
    try:
        with pytest.raises(RuntimeError):
            with store.transaction():
                task = store.subagents.create_subagent_task("source", "test", "test", "work")
                assert store.subagent_task(task["id"]) is not None
                raise RuntimeError("abort")
        assert store._db.execute("SELECT COUNT(*) FROM subagent_tasks").fetchone()[0] == 0
    finally:
        store.close()


def test_nested_failure_preserves_outer_work_and_does_not_commit(tmp_path):
    store = Store(tmp_path / "db")
    try:
        with store.transaction():
            task = store.create_subagent_task("source", "test", "outer", "work")
            with pytest.raises(ValueError):
                with store.transaction():
                    store.cancel_subagent_task(task["id"], "test")
                    raise ValueError("undo inner")
            assert store.subagent_task(task["id"])["status"] == "queued"
            assert store._db.in_transaction
        assert not store._db.in_transaction
        assert store.subagent_task(task["id"])["status"] == "queued"
    finally:
        store.close()


def test_turn_commit_rolls_back_messages_and_outbox_on_failure(tmp_path, monkeypatch):
    store = Store(tmp_path / "db")
    try:
        event = IncomingMessage("event", "message", "hello", 1, 1)
        store.add_event(event)
        def fail(*args):
            raise RuntimeError("fail after message and outbox writes")
        monkeypatch.setattr(store, "_queue_memory_operations", fail)
        with pytest.raises(RuntimeError):
            store.commit_turn([event], "hello", AgentReply(["reply"]), turn_id="turn")
        for table in ("messages", "outbox", "turns"):
            assert store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert len(store.pending_events()) == 1
    finally:
        store.close()
