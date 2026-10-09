import sqlite3

import pytest

from momoi.storage import Store
from momoi.storage.core import migrations
from momoi.memory.storage.transactions import transaction
from tests.test_memory_repository import write


def test_commit_migration_preserves_tagged_records_and_is_idempotent(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    store = Store(path)
    try:
        identifier = write(store.memories.repository)
        store.memories.repository.update_meta(store.memories.snapshots([identifier])[identifier], {'tags': ['food_drink']})
        with transaction(store._db):
            store._db.execute('DROP TABLE memory_commits')
            store._db.execute(f'PRAGMA user_version={migrations.MIGRATIONS.index(migrations._add_memory_commits)}')
        tables = ('memories', 'memory_evidence', 'memory_tombstones', 'semantic_spaces',
                  'semantic_documents', 'semantic_dirty_sources', 'memory_operation_batches')
        before = {table: [tuple(row) for row in store._db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                  for table in tables}
    finally:
        store.close()
    store = Store(path)
    try:
        assert store._db.execute('PRAGMA user_version').fetchone()[0] == migrations.SCHEMA_VERSION
        assert {table: [tuple(row) for row in store._db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in tables} == before
        assert store._db.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0
        migrations.apply_migrations(store._db)
        assert {table: [tuple(row) for row in store._db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in tables} == before
    finally:
        store.close()


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
