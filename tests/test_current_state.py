import sqlite3

import pytest

from momoi.storage import Store
from momoi.models import AgentReply
from momoi.storage.current_state import CurrentStateManager, SlotInput, StateConflict


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    now = [1000.0]
    manager = CurrentStateManager(store._db, clock=lambda: now[0])
    yield manager, now, store
    store.close()


def add(manager, *, ttl=60, operation="a", **kwargs):
    return manager.apply(
        add=[SlotInput("owner", "availability", "in a meeting", ttl)],
        source_turn_id="turn-a",
        operation_id=operation,
        expected_revision=manager.snapshot().revision,
        **kwargs,
    )


def test_expiry_boundary_read_filter_and_physical_cleanup(state):
    manager, now, store = state
    created = add(manager)
    now[0] = 1059.999
    assert len(manager.snapshot().slots) == 1
    now[0] = 1060
    assert manager.snapshot().slots == ()
    assert (
        store._db.execute("SELECT COUNT(*) FROM current_state_slots").fetchone()[0] == 1
    )
    result = manager.expire(operation_id="expiry", expected_revision=1)
    assert result.removed == created.added
    assert (
        store._db.execute("SELECT COUNT(*) FROM current_state_slots").fetchone()[0] == 0
    )


def test_replace_is_atomic_and_conflicts_leave_everything_unchanged(state):
    manager, _, _ = state
    old = add(manager).added[0]
    before = manager.snapshot()
    with pytest.raises(StateConflict, match="dimension"):
        add(manager, operation="duplicate")
    with pytest.raises(StateConflict, match="slot_not_found"):
        manager.apply(
            delete=[old.id, "missing"],
            source_turn_id="b",
            operation_id="bad",
            expected_revision=1,
        )
    assert manager.snapshot() == before
    assert len(manager.history()) == 1
    new = manager.apply(
        delete=[old.id],
        add=[SlotInput("owner", "availability", "free", 30)],
        source_turn_id="b",
        operation_id="replace",
        expected_revision=1,
    )
    assert new.removed == (old,)
    assert new.added[0].id != old.id
    assert manager.snapshot().slots == new.added


def test_retry_is_idempotent_without_refreshing_ttl(state):
    manager, now, _ = state
    request = dict(
        add=[SlotInput("owner", "availability", "busy", 60)],
        source_turn_id="a",
        operation_id="same",
        expected_revision=0,
    )
    first = manager.apply(**request)
    now[0] += 90
    assert manager.apply(**request) == first
    assert manager.snapshot().slots == ()
    with pytest.raises(StateConflict, match="operation_id_reused"):
        manager.apply(**{**request, "source_turn_id": "b"})


def test_separate_connections_reject_stale_snapshots(state):
    manager, now, store = state
    path = store._db.execute("PRAGMA database_list").fetchone()[2]
    db = sqlite3.connect(path)
    other = CurrentStateManager(db, clock=lambda: now[0])
    old = other.snapshot()
    add(manager)
    try:
        with pytest.raises(StateConflict, match="stale_revision"):
            other.apply(
                source_turn_id="b", operation_id="b", expected_revision=old.revision
            )
        assert other.snapshot() == manager.snapshot()
    finally:
        db.close()


@pytest.mark.parametrize(
    "ttl", [0, -1, True, 1.5, float("nan"), float("inf"), 604801, "60"]
)
def test_invalid_ttl_is_rejected_without_a_change(state, ttl):
    manager, _, _ = state
    with pytest.raises(ValueError, match="invalid_ttl"):
        add(manager, ttl=ttl)
    assert manager.snapshot().revision == 0


@pytest.mark.parametrize(
    "slot",
    [
        SlotInput("", "key", "value", 1),
        SlotInput("owner", "Bad key", "value", 1),
        SlotInput("owner", "key", "  ", 1),
        SlotInput("owner", "key", "v" * 513, 1),
    ],
)
def test_invalid_slot_values(state, slot):
    manager, _, _ = state
    with pytest.raises(ValueError):
        manager.apply(
            add=[slot], source_turn_id="a", operation_id="a", expected_revision=0
        )
    assert not manager.history()


def test_subjects_capacity_and_expired_dimension_reuse(state):
    manager, now, _ = state
    manager.MAX_SLOTS = 2
    add(manager, ttl=1)
    manager.apply(
        add=[SlotInput("assistant", "availability", "free", 100)],
        source_turn_id="b",
        operation_id="b",
        expected_revision=1,
    )
    with pytest.raises(ValueError, match="capacity"):
        manager.apply(
            add=[SlotInput("owner", "location", "home", 10)],
            source_turn_id="c",
            operation_id="c",
            expected_revision=2,
        )
    now[0] += 1
    add(manager, operation="reuse")
    assert len(manager.snapshot().slots) == 2
    assert len(manager.history()[-1].removed) == 1


def test_restore_preserves_original_ttl_and_can_undo_deletions(state):
    manager, now, _ = state
    original = add(manager).added[0]
    manager.apply(
        delete=[original.id],
        source_turn_id="b",
        operation_id="delete",
        expected_revision=1,
    )
    now[0] += 20
    restored = manager.restore(1, operation_id="restore", expected_revision=2)
    assert restored.added == (original,)
    assert manager.snapshot().slots == (original,)
    now[0] += 40
    manager.restore(1, operation_id="expired-restore", expected_revision=3)
    assert manager.snapshot().slots == ()
    with pytest.raises(ValueError, match="future_revision"):
        manager.restore(100, operation_id="future", expected_revision=4)


def test_reopen_existing_database_preserves_slots_and_other_data(tmp_path):
    path = tmp_path / "old.sqlite3"
    store = Store(path)
    store.commit_turn([], "existing history", AgentReply([]), turn_id="old")
    # Simulate an existing database created before the additive tables existed.
    for table in (
        "current_state_slots",
        "current_state_changes",
        "current_state_revision",
    ):
        store._db.execute(f"DROP TABLE {table}")
    store._db.commit()
    store.close()
    store = Store(path)
    change = add(store.current_state)
    store.close()
    store = Store(path)
    try:
        assert store.current_state.snapshot().slots == change.added
        assert (
            store._db.execute("SELECT COUNT(*) FROM turns WHERE id='old'").fetchone()[0]
            == 1
        )
        assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()


def test_transaction_failure_rolls_back_deletes_history_and_revision(state):
    manager, _, store = state
    old = add(manager).added[0]
    store._db.execute(
        "CREATE TRIGGER reject_state BEFORE INSERT ON current_state_changes BEGIN SELECT RAISE(ABORT,'test failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        manager.apply(
            delete=[old.id], source_turn_id="b", operation_id="b", expected_revision=1
        )
    assert manager.snapshot().slots == (old,)
    assert manager.snapshot().revision == 1


def test_manager_does_not_commit_callers_transaction(state):
    manager, _, store = state
    store._db.execute("BEGIN")
    with pytest.raises(RuntimeError, match="own_transaction"):
        add(manager)
    assert store._db.in_transaction
    store._db.rollback()
