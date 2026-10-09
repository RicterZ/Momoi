import asyncio
import sqlite3
from contextlib import closing
import pytest

from momoi.storage import Store
from momoi.storage.core.migrations import MIGRATIONS, _add_memory_metadata, apply_migrations
from momoi.memory.storage.transactions import transaction
from momoi.memory.storage.vectors import encode_vector
from momoi.runtime.workflows.memory_operation.contracts import MEMORY_OPERATION_FINISH_SPEC
from tests.test_memory_repository import write
from tests.test_memory_operations import store, event, submit, write as decision, apply


def test_host_classifies_with_validated_catalog_and_preserves_batch_atomicity(store):
    source = event(store)
    submit(store, source)
    batch = store.claim_memory_operation('source')
    command = decision(source)
    command['memory']['meta'] = {'tags': ['food_drink']}
    apply(store, batch, [command])
    result = asyncio.run(store.memories.search('喝茶', filters={'tags_any': ['food_drink']}))
    assert len(result) == 1 and result[0]['meta'] == {'tags': ['food_drink'], 'scope': ''}
    schema = MEMORY_OPERATION_FINISH_SPEC['input_schema']['properties']['decisions']['items']['properties']['memory']
    assert set(schema['properties']['meta']['properties']['tags']['items']['enum']) == set(store.memories.repository.tags.tags)


def test_host_rejects_invented_model_tag(store):
    source = event(store)
    submit(store, source)
    batch = store.claim_memory_operation('source')
    command = decision(source)
    command['memory']['meta'] = {'tags': ['invented']}
    with pytest.raises(ValueError, match='predefined'):
        apply(store, batch, [command])
    assert store.memories.repository.inventory() == []
    assert store._db.execute("SELECT state FROM memory_operation_batches WHERE id='source'").fetchone()[0] == 'running'


def test_tag_update_preserves_vector_and_does_not_dirty_index(store):
    identifier = write(store.memories.repository)
    space = store.ensure_semantic_space(model='fake', dimensions=2, calibration_profile='test')
    with transaction(store._db):
        store._db.execute(
            """INSERT INTO semantic_documents(space_id,document_type,source_id,chunk_index,
               content,content_sha256,state,vector,dimensions,created_at,updated_at)
               VALUES (?,'confirmed_memory',?,0,'coffee','hash','ready',?,2,0,0)""",
            (space['id'], str(identifier), encode_vector([1, 0], 2)),
        )
        store._db.execute('DELETE FROM semantic_dirty_sources')
    before = [tuple(row) for row in store._db.execute('SELECT * FROM semantic_documents')]
    original = store.memories.snapshots([identifier])[identifier]
    store.memories.repository.update_meta(original, {'tags': ['food_drink']})
    assert store._db.execute('SELECT COUNT(*) FROM semantic_dirty_sources').fetchone()[0] == 0
    assert [tuple(row) for row in store._db.execute('SELECT * FROM semantic_documents')] == before
    assert store.memories.snapshots([identifier])[identifier]['updated_at'] >= original['updated_at']


def test_metadata_migration_reopen_preserves_records_evidence_and_vectors(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    store = Store(path)
    try:
        identifier = write(store.memories.repository)
        forgotten = write(store.memories.repository, key='forgotten')
        store.memories.repository.forget(store.memories.snapshots([forgotten])[forgotten],
                            {'event_id': 'forget', 'quote': 'forget'}, now=1)
        space = store.ensure_semantic_space(model='fake', dimensions=2, calibration_profile='test')
        with transaction(store._db):
            store._db.execute(
                """INSERT INTO semantic_documents(space_id,document_type,source_id,chunk_index,
                   content,content_sha256,state,vector,dimensions,created_at,updated_at)
                   VALUES (?,'confirmed_memory',?,0,'coffee','hash','ready',?,2,0,0)""",
                (space['id'], str(identifier), encode_vector([1, 0], 2)),
            )
            store._db.execute('ALTER TABLE memories DROP COLUMN meta_json')
            store._db.execute(f'PRAGMA user_version={MIGRATIONS.index(_add_memory_metadata)}')
        columns = ','.join(row[1] for row in store._db.execute('PRAGMA table_info(memories)'))
        queries = {'memories': f'SELECT {columns} FROM memories ORDER BY id'}
        queries.update({table: f'SELECT * FROM {table} ORDER BY rowid'
                        for table in ('memory_evidence', 'memory_tombstones', 'semantic_documents')})
        before = {table: [tuple(row) for row in store._db.execute(query)] for table, query in queries.items()}
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert {table: [tuple(row) for row in reopened._db.execute(query)]
                for table, query in queries.items()} == before
        assert reopened._db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert reopened._db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert reopened.memories.repository.active('preference', 'drink')['meta'] == {'tags': [], 'scope': ''}
        assert reopened.memories.snapshots([forgotten]) == {}
        assert asyncio.run(reopened.memories.search('无糖咖啡'))
        assert asyncio.run(reopened.memories.search('无糖咖啡', filters={'tags_any': ['food_drink']})) == []
        before = [tuple(row) for row in reopened._db.execute('SELECT * FROM memories')]
        apply_migrations(reopened._db)
        assert [tuple(row) for row in reopened._db.execute('SELECT * FROM memories')] == before
    finally:
        reopened.close()


def test_metadata_migration_rolls_back_with_version_on_failure(tmp_path, monkeypatch):
    from momoi.storage.core import migrations

    with closing(sqlite3.connect(tmp_path / 'rollback.sqlite3')) as db:
        db.execute('CREATE TABLE memories(id INTEGER PRIMARY KEY)')
        db.commit()
        def fail(connection):
            _add_memory_metadata(connection)
            raise RuntimeError('after DDL')
        monkeypatch.setattr(migrations, 'MIGRATIONS', (fail,))
        with pytest.raises(RuntimeError, match='after DDL'):
            migrations.apply_migrations(db)
        assert [row[1] for row in db.execute('PRAGMA table_info(memories)')] == ['id']
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
