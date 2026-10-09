import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from momoi.memory import Memory, PlanningContext
from momoi.memory.retrieval.dense import VectorMemoryEvidence
from momoi.memory.storage.transactions import transaction
from momoi.storage import Store
from momoi.storage.core.schema import execute_schema
from momoi.storage.core.migrations import MIGRATIONS, apply_migrations, migration_transaction
from momoi.storage.core.memory_scope_migration import add_memory_scope, scope_preflight
from momoi.storage.core.memory_scope_audit import audit, digest
from tests.test_memory_repository import database
from tests.test_memory_operations import event, submit, write as command


def write(memory, scope='', key='drink'):
    source = {'event_id': 'owner', 'quote': 'coffee'}
    return memory.repository.write(
        {'kind': 'preference', 'key': key, 'content': 'coffee',
         'activation': 'scoped' if scope else 'recall', 'expires_at': None,
         'meta': {'tags': [], 'scope': scope}}, [], source, [source], now=1,
    )


def test_same_key_scope_isolates_read_forget_readd_and_dense_pool(database):
    dense = AsyncMock(return_value=VectorMemoryEvidence({}, {}))
    memory = Memory(database, dense_recall=dense)
    global_id, goal, heartbeat = [write(memory, scope) for scope in ('', 'goal:abc', 'heartbeat')]
    assert [row['id'] for row in asyncio.run(memory.search('coffee'))] == [global_id]
    result = asyncio.run(memory.search('coffee', filters={'scope': 'goal:abc'}))
    assert [row['id'] for row in result] == [goal]
    assert result[0]['meta']['scope'] == 'goal:abc'
    assert dense.call_args.kwargs['eligible_ids'] == {'coffee': frozenset({str(goal)})}
    assert [row['id'] for row in memory.search_literal('coffee', 10, filters={'scope': 'heartbeat'})] == [heartbeat]
    assert [row['id'] for row in memory.search_literal('coffee', 10, include_scoped=True, filters={'scope': ''})] == [global_id]
    memory.repository.forget(memory.snapshots([goal])[goal], {'event_id': 'forget', 'quote': 'forget'}, now=2)
    assert set(memory.snapshots([global_id, goal, heartbeat])) == {global_id, heartbeat}
    assert memory.repository.tombstone('preference', 'drink', scope='goal:abc')
    assert not memory.repository.tombstone('preference', 'drink')
    # Repository is the trusted low-level API; public apply requires fresh evidence.
    replacement = write(memory, 'goal:abc')
    assert set(memory.snapshots([global_id, goal, heartbeat, replacement])) == {global_id, heartbeat, replacement}
    assert memory.repository.active('preference', 'drink', scope='heartbeat')['id'] == heartbeat


def test_one_plan_can_create_same_key_in_distinct_scopes(database):
    memory = Memory(database)
    requests, decisions = [], []
    for i, scope in enumerate(('', 'heartbeat')):
        requests.append({'id': str(i), 'type': 'add', 'content': 'coffee', 'event_id': 'owner', 'evidence': 'coffee', 'scope': scope})
        decisions.append({'operation_ids': [str(i)], 'action': 'write', 'reason': '不同使用范围', 'target_ids': [],
                          'memory': {'kind': 'preference', 'key': 'drink', 'content': 'coffee',
                                     'activation': 'scoped' if scope else 'recall', 'expires_at': None,
                                     'meta': {'tags': [], 'scope': scope}},
                          'evidence': [{'event_id': 'owner', 'quote': 'coffee'}]})
    plan = memory.writing.review(PlanningContext(requests, {'owner': 'coffee'}, {}), {'decisions': decisions})
    memory.apply(plan, operation_id='scopes')
    assert {row['meta']['scope'] for row in memory.repository.inventory()} == {'', 'heartbeat'}


def test_scope_change_invalidates_snapshot_and_metadata_cannot_move_it(database):
    memory = Memory(database)
    identifier = write(memory, 'heartbeat')
    snapshot = memory.snapshots([identifier])[identifier]
    with pytest.raises(ValueError, match='cannot change memory scope'):
        memory.repository.update_meta(snapshot, {'tags': [], 'scope': 'webhook'})
    memory.repository.update_meta(snapshot, {'tags': []})
    assert memory.snapshots([identifier])[identifier]['meta']['scope'] == 'heartbeat'
    with transaction(database):
        database.execute("UPDATE memories SET scope_key='webhook' WHERE id=?", (identifier,))
    with pytest.raises(ValueError, match='snapshot_changed'):
        memory.repository.validate_snapshots({identifier: snapshot})


def test_dashboard_forget_and_index_invalidation_are_scope_specific(tmp_path):
    store = Store(tmp_path / 'db.sqlite3')
    try:
        first, second = write(store.memories), write(store.memories, 'heartbeat')
        with store.transaction():
            store._db.execute('DELETE FROM semantic_dirty_sources')
        assert store.forget_memory_by_id(second, 'forget heartbeat only')
        assert store.memories.repository.active('preference', 'drink')['id'] == first
        assert {row['id'] for row in store.list_memories()} == {first}
        assert [row[0] for row in store._db.execute('SELECT source_id FROM semantic_dirty_sources')] == [str(second)]
        with store.transaction():
            store._db.execute('DELETE FROM semantic_dirty_sources')
            store._db.execute("UPDATE memories SET scope_key='webhook' WHERE id=?", (second,))
        assert [row[0] for row in store._db.execute('SELECT source_id FROM semantic_dirty_sources')] == [str(second)]
    finally:
        store.close()


def test_host_rejects_invented_scope_but_accepts_existing_goal(tmp_path):
    store = Store(tmp_path / 'db.sqlite3')
    try:
        source = event(store)
        submit(store, source)
        batch = store.claim_memory_operation('source')
        decision = command(source)
        decision['memory'].update(activation='scoped', meta={'scope': 'goal:' + 'a' * 32, 'tags': []})
        ctx = PlanningContext(batch['operations'], {source.event_id: source.text}, {})
        plan = store.memories.writing.review(ctx, {'decisions': [decision]})
        with pytest.raises(ValueError, match='supplied Momoi workflow'):
            store.apply_memory_operation(batch, plan)
        assert store.memories.repository.inventory() == []
        with store.transaction():
            store._db.execute('''INSERT INTO goals(id,title,success_criteria,source_event_id,status,plan_json,created_at,updated_at)
                VALUES (?,'coffee','coffee',?,'active','[]',1,1)''', ('a' * 32, source.event_id))
        store.apply_memory_operation(batch, plan)
        assert store.memories.repository.inventory()[0]['meta']['scope'] == 'goal:' + 'a' * 32
        assert store.memories.repository.inventory()[0]['key'] == 'drink'
        store.validate_memory_scopes([], [{**decision, 'memory': {**decision['memory'], 'meta': {'scope': 'heartbeat'}}}])
    finally:
        store.close()


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / 'old.sqlite3'
    db = sqlite3.connect(path)
    schema = (Path(__file__).parents[1] / 'src/momoi/storage/core/schema.sql').read_text()
    schema = schema.replace(",\n    scope_key TEXT NOT NULL DEFAULT ''\n", '\n')
    schema = schema.replace("    scope_key TEXT NOT NULL DEFAULT '',\n", '')
    schema = schema.replace('PRIMARY KEY (scope_key, kind, key)', 'PRIMARY KEY (kind, key)')
    execute_schema(db, schema)
    db.execute(f'PRAGMA user_version={MIGRATIONS.index(add_memory_scope)}')
    db.commit()
    yield db, path
    db.close()


def old_memory(db, key='drink', activation='recall', kind='preference'):
    return db.execute("""INSERT INTO memories(kind,key,content,activation,authority,source_event_id,
        evidence_quote,created_at,updated_at,meta_json) VALUES (?,?,?,?,'owner','owner','coffee',1,1,?)""",
        (kind, key, 'coffee', activation, '{"tags":["food_drink"]}')).lastrowid


def test_scope_migration_rehearsal_preserves_ids_evidence_history_and_source(legacy):
    db, path = legacy
    identifiers = [old_memory(db), old_memory(db, 'heartbeat.drink', 'scoped'),
                   old_memory(db, 'goal.' + 'a' * 32 + '.drink', 'scoped')]
    old = old_memory(db, 'webhook.drink', 'scoped')
    db.execute('UPDATE memories SET superseded_by=? WHERE id=?', (identifiers[1], old))
    db.execute("INSERT INTO memory_evidence(memory_id,source_event_id,quote,created_at) VALUES (?,'owner','coffee',1)", (old,))
    db.execute("INSERT INTO memory_tombstones VALUES ('preference','heartbeat.drink','forget','forget',2)")
    db.commit()
    before = digest(db)
    result = audit(path)
    assert result['preflight']['ready'] and result['restore'] == result['repeat'] == 'ok'
    assert digest(db) == before and db.execute('PRAGMA user_version').fetchone()[0] == 35
    apply_migrations(db)
    assert db.execute('PRAGMA user_version').fetchone()[0] == 36
    assert db.execute('SELECT id,scope_key,key FROM memories ORDER BY id').fetchall() == [
        (identifiers[0], '', 'drink'), (identifiers[1], 'heartbeat', 'drink'),
        (identifiers[2], 'goal:' + 'a' * 32, 'drink'), (old, 'webhook', 'drink')]
    assert db.execute('SELECT superseded_by FROM memories WHERE id=?', (old,)).fetchone()[0] == identifiers[1]
    store = Store(path)
    try:
        assert set(store.memories.snapshots(identifiers)) == {identifiers[0], identifiers[2]}
        assert store.memories.repository.snapshots([identifiers[2]])[identifiers[2]]['meta'] == {'scope': 'goal:' + 'a' * 32, 'tags': ['food_drink']}
    finally:
        store.close()


@pytest.mark.parametrize('problem', ['prefix', 'global_prefix', 'kind', 'recent', 'duplicate', 'tombstone', 'pending', 'maintenance'])
def test_migration_blockers_do_not_change_database(legacy, problem):
    db, path = legacy
    old_memory(db)
    if problem == 'prefix':
        old_memory(db, 'goal.bad.drink', 'scoped')
    elif problem == 'global_prefix':
        old_memory(db, 'heartbeat.drink')
    elif problem == 'kind':
        old_memory(db, 'unknown', kind='shared')
    elif problem == 'recent':
        old_memory(db, 'old', activation='recent')
    elif problem == 'duplicate':
        old_memory(db)
    elif problem == 'tombstone':
        db.execute("INSERT INTO memory_tombstones VALUES ('preference','goal.bad.drink','forget','forget',2)")
    elif problem == 'pending':
        db.execute("INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at) VALUES ('pending','owner','owner','[]','completed',1,1)")
        db.execute("INSERT INTO memory_operation_batches(id,operations_json,context_json,events_json,conversation_json,created_at,updated_at) VALUES ('pending','[]','[]','[]','[]',1,1)")
    else:
        db.execute("INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at) VALUES ('maint','autonomous','memory_maintenance','[]','running',1,1)")
    db.commit()
    before = digest(db)
    assert not scope_preflight(db)['ready']
    assert not audit(path)['preflight']['ready']
    with pytest.raises(ValueError, match='migration blocked'):
        apply_migrations(db)
    assert digest(db) == before
    assert db.execute('PRAGMA user_version').fetchone()[0] == 35


def test_ddl_failure_rolls_back_identity_conversion(legacy):
    db, _ = legacy
    old_memory(db, 'heartbeat.drink', 'scoped')
    db.commit()
    before = digest(db)
    def deny(action, arg1, arg2, name, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE and arg1 == 'memory_tombstones' else sqlite3.SQLITE_OK
    db.set_authorizer(deny)
    with pytest.raises(sqlite3.DatabaseError):
        apply_migrations(db)
    db.set_authorizer(None)
    assert digest(db) == before
    assert db.execute('PRAGMA user_version').fetchone()[0] == 35


def test_maintenance_groups_do_not_treat_same_key_as_same_scope(database):
    from momoi.runtime.workflows.memory_maintenance.grouping import build_atomic_memory_groups
    memory = Memory(database)
    global_id, first, second = [write(memory, scope) for scope in ('', 'heartbeat', 'webhook')]
    groups = build_atomic_memory_groups(memory.repository.inventory(), [global_id, first, second],
                                        forced_groups=[[global_id, first, second]])
    assert groups == [[global_id], [first], [second]]
    with pytest.raises(ValueError, match='cross scopes'):
        memory.repository.merge(first, [second], 'coffee', 'scoped', None,
                                [{'id': 'event', 'content': 'coffee', 'occurred_at': 2}])
    assert set(memory.snapshots([global_id, first, second])) == {global_id, first, second}


def test_migration_requeues_changed_scoped_documents_and_preserves_index_data(legacy):
    db, path = legacy
    old_memory(db, 'heartbeat.drink', 'scoped')
    # Dequeue the original insert: migration must independently invalidate this ID.
    db.execute('DELETE FROM semantic_dirty_sources')
    db.execute('CREATE INDEX tombstone_source ON memory_tombstones(source_event_id)')
    db.commit()
    assert audit(path)['preflight']['ready']
    apply_migrations(db)
    assert db.execute('SELECT source_type,source_id FROM semantic_dirty_sources').fetchall() == [('confirmed_memory', '1')]
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='tombstone_source'").fetchone()
    db.execute("DELETE FROM semantic_dirty_sources")
    db.execute("INSERT INTO memory_tombstones VALUES ('heartbeat','preference','drink','forget','forget',2)")
    assert db.execute('SELECT source_id FROM semantic_dirty_sources').fetchall() == [('1',)]


def test_audit_cli_runs_outside_repository_and_leaves_source_untouched(legacy):
    import json
    import subprocess
    import sys
    db, path = legacy
    old_memory(db, 'heartbeat.drink', 'scoped')
    db.commit()
    before = path.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'momoi.storage.core.memory_scope_audit', str(path)],
                            cwd=path.parent, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['restore'] == 'ok'
    assert path.read_bytes() == before


@pytest.mark.parametrize('retirement', ['forgotten', 'superseded', 'expired'])
def test_scope_migration_preserves_retired_legacy_rows_without_reviving_them(legacy, retirement):
    db, path = legacy
    current = old_memory(db)
    identifier = old_memory(db, 'old', activation='recent', kind='episodic')
    if retirement == 'forgotten':
        db.execute("INSERT INTO memory_tombstones VALUES ('episodic','old','forget','forgotten',2)")
    elif retirement == 'superseded':
        db.execute('UPDATE memories SET superseded_by=? WHERE id=?', (current, identifier))
    else:
        db.execute('UPDATE memories SET expires_at=2 WHERE id=?', (identifier,))
    # Alias tombstones can outlive the rows they hid; their original identity must survive.
    db.execute("INSERT INTO memory_tombstones VALUES ('routine','removed','forget','forgotten',2)")
    db.commit()
    assert audit(path)['preflight']['ready']
    apply_migrations(db)
    assert db.execute('SELECT kind,activation FROM memories WHERE id=?', (identifier,)).fetchone() == ('episodic', 'recent')
    store = Store(path)
    try:
        assert set(store.memories.snapshots([current, identifier])) == {current}
        assert store._db.execute("SELECT scope_key FROM memory_tombstones WHERE kind='routine'").fetchone()[0] == ''
    finally:
        store.close()


def test_unknown_retired_kind_remains_a_migration_blocker(legacy):
    db, _ = legacy
    old_memory(db, 'old', kind='invented')
    db.execute("INSERT INTO memory_tombstones VALUES ('invented','old','forget','forgotten',2)")
    db.commit()
    report = scope_preflight(db)
    assert not report['ready']
    assert len(report['issues']) == 2
