import sqlite3

import pytest

from momoi.storage import Store
from momoi.storage.core import migrations
from momoi.memory.storage.transactions import transaction
from tests.test_memory_repository import write
from scripts.check_memory_metadata_migration import audit, fingerprint


def test_commit_migration_preserves_tagged_records_and_backup_recovery(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    store = Store(path)
    try:
        identifier = write(store.memory)
        store.memory.update_meta(store.memories.snapshots([identifier])[identifier], {'tags': ['food_drink']})
        with transaction(store._db):
            store._db.execute('DROP TABLE memory_commits')
            store._db.execute(f'PRAGMA user_version={migrations.MIGRATIONS.index(migrations._add_memory_commits)}')
        before = fingerprint(store._db, include_meta=True)
    finally:
        store.close()
    original_bytes = path.read_bytes()
    report = audit(path, stage='commits')
    assert path.read_bytes() == original_bytes
    assert report['from_version'] == 34 and report['to_version'] == 35
    assert report['tables'] == before
    assert report['restore'] == 'ok'
    store = Store(path)
    try:
        assert store._db.execute('PRAGMA user_version').fetchone()[0] == 35
        assert fingerprint(store._db, include_meta=True) == before
        assert store._db.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0
        migrations.apply_migrations(store._db)
        assert fingerprint(store._db, include_meta=True) == before
    finally:
        store.close()
    with pytest.raises(ValueError, match='expected pre-commits'):
        audit(path, stage='commits')


def test_commit_migration_failure_rolls_back_table_and_version(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / 'rollback.sqlite3')
    try:
        def fail(connection):
            migrations._add_memory_commits(connection)
            raise RuntimeError('after receipts DDL')
        monkeypatch.setattr(migrations, 'MIGRATIONS', (fail,))
        with pytest.raises(RuntimeError, match='after receipts DDL'):
            migrations.apply_migrations(db)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_commits'").fetchone() is None
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
    finally:
        db.close()
