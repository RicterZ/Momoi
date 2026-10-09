from momoi.storage import Store
from momoi.storage.core.migrations import SCHEMA_VERSION
from momoi.config.loading import remove_retired_thinking_stages


def test_upgrade_cancels_pending_maintenance_and_preserves_history(tmp_path):
    path = tmp_path / 'momoi.sqlite3'
    store = Store(path)
    for identifier in ('pending', 'completed', 'uncertain'):
        store.begin_turn(identifier, 'memory_maintenance', [])
    store._db.execute("UPDATE turns SET state='completed' WHERE id='completed'")
    store._db.execute("UPDATE turns SET state='needs_reconciliation' WHERE id='uncertain'")
    store.append_turn_journal('completed', 'memory_maintenance_complete', {'summary': 'old review'})
    store._db.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
    store._db.commit()
    store.close()
    store = Store(path)
    try:
        states = dict(store._db.execute('SELECT id,state FROM turns'))
        assert states == {'pending': 'cancelled', 'completed': 'completed', 'uncertain': 'needs_reconciliation'}
        assert store._db.execute("SELECT COUNT(*) FROM turn_journal WHERE turn_id='completed'").fetchone()[0] == 1
        assert not hasattr(store, 'queue_memory_maintenance_turn')
    finally:
        store.close()


def test_retired_config_stage_is_removed_without_touching_other_settings():
    raw = {'thinking': {'stages': {'memory_maintenance': 'low', 'owner': 'high'}}}
    remove_retired_thinking_stages(raw)
    assert raw == {'thinking': {'stages': {'owner': 'high'}}}
