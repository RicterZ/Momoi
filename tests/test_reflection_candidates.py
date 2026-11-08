import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from momoi.config.models import ReflectionConfig
from momoi.storage import Store
from tests.test_weekly_reflection_storage import seed, stamp


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'db', timezone='Asia/Shanghai')
    yield value
    value.close()


def review(store, end, days, *, key='quiet', previous=(), content='老师选座时偏好安静的位置', triggers=None):
    for day in days:
        seed(store, day, content, key=key)
    row = store.claim_due_weekly_reflection(ReflectionConfig(enabled=True), stamp(end + 'T05:00:00'))
    source = json.loads(row['input_json'])
    events = [{'refs': [item['id']], 'summary': '一次独立选座'}
              for day in source['days'] for item in day['observations']]
    events += [{'refs': [item['id']], 'summary': item['summary']} for item in previous]
    finding = {'key': key, 'kind': 'preference', 'content': content, 'events': events, 'conflicts': [], 'triggers': triggers or []}
    store.commit_weekly_reflection(end, 'unused', {'summary': '累计观察', 'findings': [finding]})
    return finding


def finish_admission(store, receipt, *, batch=None):
    import hashlib
    from momoi.models import IncomingMessage
    from tests.test_memory_operations import apply, write
    batch = batch or store.claim_memory_operation(receipt['operation_id'])
    candidate = store._db.execute('SELECT * FROM reflection_candidates WHERE memory_operation_id=?',
                                 (batch['id'],)).fetchone()
    source = IncomingMessage(**batch['events'][0])
    decision = write(source, ids=['admit'], content=candidate['content'],
                     key='reflection.' + hashlib.sha256(candidate['key'].encode()).hexdigest()[:24])
    decision['memory']['meta'] = {'tags': [], 'triggers': json.loads(candidate['triggers_json']), 'scope': ''}
    apply(store, batch, [decision])
    return store._db.execute('SELECT memory_id FROM reflection_candidates WHERE id=?', (candidate['id'],)).fetchone()[0]


def test_cross_week_counts_expire_and_do_not_enter_memory(store):
    review(store, '2026-10-12', ['2026-10-06', '2026-10-07'])
    old = store.reflection_candidates(end='2026-10-12')[0]
    assert old['count'] == 2 and old['status'] == 'observation'
    # Omitted old events survive; new week carries compact observations instead of old daily transcripts.
    review(store, '2026-10-19', ['2026-10-12', '2026-10-13', '2026-10-14'])
    source = json.loads(store.weekly_reflection('2026-10-19')['input_json'])
    assert len(source['previous_candidates'][0]['events']) == 2
    assert source['days'][0]['date'] == '2026-10-12'
    candidate = store.reflection_candidates(end='2026-10-19')[0]
    assert candidate['count'] == 5 and candidate['status'] == 'pending'
    assert store.memories.repository.inventory() == []
    assert store.reflection_candidates(end='2026-11-08')[0]['count'] == 3
    assert store.reflection_candidates(end='2026-11-15') == []


def test_edit_delete_and_admit_are_revision_checked_and_atomic(store):
    review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
    item = store.reflection_candidates(end='2026-10-12')[0]
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
        store.change_reflection_candidate(item['id'], item['revision'], content='老师选座时喜欢安静一点')
        with pytest.raises(ValueError, match='changed'):
            store.change_reflection_candidate(item['id'], item['revision'], admit=True)
        current = store.reflection_candidates(end='2026-10-12')[0]
        result = store.change_reflection_candidate(current['id'], current['revision'], admit=True)
        assert store.change_reflection_candidate(current['id'], current['revision'], admit=True) == result
        assert store.memories.repository.inventory() == []
        assert store.reflection_candidates(end='2026-10-12')[0]['review_state'] == 'pending'
        finish_admission(store, result)
        assert store.reflection_candidates() == []
    memories = store.memories.repository.inventory()
    assert len(memories) == 1 and memories[0]['activation'] == 'recall'
    assert memories[0]['content'] == '老师选座时喜欢安静一点'
    assert memories[0]['source_event_id'].startswith('dashboard:reflection:')
    assert memories[0]['meta']['triggers'] == []


def test_frozen_week_cannot_overwrite_edit_or_revive_deleted_candidate(store):
    review(store, '2026-10-12', ['2026-10-06', '2026-10-07'])
    original = store.reflection_candidates(end='2026-10-12')[0]
    seed(store, '2026-10-12', '本周选座')
    record = store.claim_due_weekly_reflection(ReflectionConfig(enabled=True), stamp('2026-10-19T05:00:00'))
    source = json.loads(record['input_json'])
    finding = {'key': 'quiet', 'kind': 'preference', 'content': '模型想改写',
               'events': [{'refs': [source['days'][0]['observations'][0]['id']], 'summary': '新选座'}], 'conflicts': []}
    store.change_reflection_candidate(original['id'], original['revision'], content='用户修订正文')
    store.commit_weekly_reflection('2026-10-19', 'unused', {'summary': '归纳', 'findings': [finding]})
    current = store.reflection_candidates(end='2026-10-19')[0]
    assert current['content'] == '用户修订正文' and current['count'] == 3
    store.change_reflection_candidate(current['id'], current['revision'], delete=True)
    with store.transaction():
        store.apply_reflection_candidates(source, [finding])
    assert store.reflection_candidates(end='2026-10-19') == []


def test_remention_does_not_refresh_date_or_allow_split_support(store):
    review(store, '2026-10-12', ['2026-10-06'])
    assert store.reflection_candidates(end='2026-10-12') == []
    previous = store.reflection_candidates(end='2026-10-12', include_seeds=True)[0]
    seed(store, '2026-10-12', '又聊起上次选座')
    source = store.weekly_reflection_source('2026-10-19')
    new = source['days'][0]['observations'][0]['id']
    finding = {'key': 'quiet', 'kind': 'preference', 'content': previous['content'],
               'events': [{'refs': [previous['events'][0]['id'], new], 'summary': '同一次选座'}], 'conflicts': []}
    with store.transaction():
        store.apply_reflection_candidates(source, [finding])
    item = store.reflection_candidates(end='2026-10-19', include_seeds=True)[0]
    assert item['count'] == 1 and item['events'][0]['date'] == '2026-10-06'
    bad = deepcopy(finding)
    bad['events'].append({'refs': [new], 'summary': '不能拆成第二次'})
    with pytest.raises(ValueError, match='counted_twice'), store.transaction():
        store.apply_reflection_candidates(source, [bad])
    assert store.reflection_candidates(end='2026-10-19', include_seeds=True)[0] == item


def test_expired_or_conflicting_candidate_cannot_be_admitted(store):
    review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
    item = store.reflection_candidates(end='2026-10-12')[0]
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-11-12'
        with pytest.raises(ValueError, match='not_ready'):
            store.change_reflection_candidate(item['id'], item['revision'], admit=True)
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
        with store.transaction():
            store._db.execute('UPDATE reflection_candidates SET conflicts_json=?', (json.dumps([item['events'][0]]),))
        with pytest.raises(ValueError, match='not_ready'):
            store.change_reflection_candidate(item['id'], item['revision'], admit=True)
    assert store.memories.repository.inventory() == []


def test_weekly_commit_rolls_back_all_candidates_and_rejects_unknown_references(store):
    seed(store, '2026-10-06')
    row = store.claim_due_weekly_reflection(ReflectionConfig(enabled=True), stamp('2026-10-12T05:00:00'))
    source = json.loads(row['input_json'])
    ref = source['days'][1]['observations'][0]['id']
    good = {'key': 'quiet', 'kind': 'preference', 'content': '偏好安静',
            'events': [{'refs': [ref], 'summary': '独立选座'}], 'conflicts': []}
    bad = {**good, 'key': 'other', 'events': [{'refs': ['missing'], 'summary': '编造'}]}
    with pytest.raises(ValueError, match='unknown_observation'):
        store.commit_weekly_reflection('2026-10-12', 'unused', {'summary': '失败', 'findings': [good, bad]})
    assert store.reflection_candidates(end='2026-10-12', include_seeds=True) == []
    assert store.weekly_reflection('2026-10-12')['state'] == 'running'


def test_dashboard_candidate_auth_revision_and_manual_admission(store):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from momoi.dashboard.app import create_dashboard_app
    from momoi.dashboard.auth import issue_dashboard_jwt
    from momoi.dashboard.settings import DashboardSettings

    review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])

    async def run():
        async with TestClient(TestServer(create_dashboard_app(store, token='secret', settings=DashboardSettings(())))) as client:
            url = '/api/reflection-candidates'
            assert (await client.get(url)).status == 401
            client.session.headers['Authorization'] = 'Bearer ' + issue_dashboard_jwt('secret')
            data = await (await client.get(url)).json()
            item = data['items'][0]
            path = f"{url}/{item['id']}"
            assert (await client.post(path + '/admit', json={})).status == 400
            assert (await client.patch(path, json={'revision': item['revision'], 'content': '修订偏好'})).status == 200
            assert (await client.post(path + '/admit', json={'revision': item['revision']})).status == 409
            assert (await client.post(path + '/admit', json={'revision': item['revision'] + 1})).status == 200
            queued = (await (await client.get(url)).json())['items'][0]
            assert queued['review_state'] == 'pending'
            assert store.memories.repository.inventory() == []
            assert (await client.delete(path, json={'revision': item['revision']})).status == 409
            assert (await client.patch(path, json={'revision': queued['revision'], 'content': '修改审核中正文'})).status == 409
            assert (await client.delete(path, json={'revision': queued['revision']})).status == 409
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
        asyncio.run(run())


def test_manual_admission_is_not_blocked_by_previously_deleted_memory(store):
    import hashlib
    from tests.test_memory_repository import write
    review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
    item = store.reflection_candidates(end='2026-10-12')[0]
    key = 'reflection.' + hashlib.sha256(item['key'].encode()).hexdigest()[:24]
    old = write(store.memories.repository, key=key, text=item['content'])
    store.forget_memory_by_id(old, '删除旧记录')
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
        result = store.change_reflection_candidate(item['id'], item['revision'], admit=True)
    memory_id = finish_admission(store, result)
    assert memory_id != old
    assert not store.memories.snapshots([old])
    assert store.memories.snapshots([memory_id])


def test_candidate_triggers_survive_restart_edit_and_admission(tmp_path):
    path = tmp_path / 'trigger-db'
    store = Store(path, timezone='Asia/Shanghai')
    review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'], triggers=['选座', '安静'])
    store.close()
    store = Store(path, timezone='Asia/Shanghai')
    try:
        item = store.reflection_candidates(end='2026-10-12')[0]
        assert item['triggers'] == ['选座', '安静']
        assert store.weekly_reflection_source('2026-10-19')['previous_candidates'][0]['triggers'] == item['triggers']
        with patch('momoi.storage.reflection.candidates.datetime') as clock:
            clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
            store.change_reflection_candidate(item['id'], item['revision'], content='老师选座偏好安静的位置')
            receipt = store.change_reflection_candidate(item['id'], item['revision'] + 1, admit=True)
        finish_admission(store, receipt)
        assert store.memories.repository.inventory()[0]['meta']['triggers'] == ['选座', '安静']
    finally:
        store.close()


def test_existing_candidates_migrate_with_empty_triggers(tmp_path):
    from momoi.storage.core.migrations import MIGRATIONS, _add_reflection_candidate_triggers
    import sqlite3

    path = tmp_path / 'old-db'
    store = Store(path, timezone='Asia/Shanghai')
    review(store, '2026-10-12', ['2026-10-06', '2026-10-07'])
    store.close()
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE reflection_candidates DROP COLUMN triggers_json')
        db.execute(f'PRAGMA user_version={MIGRATIONS.index(_add_reflection_candidate_triggers)}')
    store = Store(path, timezone='Asia/Shanghai')
    try:
        candidate = store.reflection_candidates(end='2026-10-12')[0]
        assert candidate['triggers'] == [] and candidate['count'] == 2
        assert candidate['content'] == '老师选座时偏好安静的位置'
    finally:
        store.close()


@pytest.mark.parametrize('action', ['write', 'noop', 'defer', 'reject'])
def test_admission_runs_private_review_against_latest_memory(daemon, action):
    import asyncio
    from momoi.models import ToolCall
    from tests.test_memory_repository import write

    store = daemon.store
    review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'],
           triggers=['选座'])
    item = store.reflection_candidates(end='2026-10-12')[0]
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
        receipt = store.change_reflection_candidate(item['id'], item['revision'], admit=True)
    assert store.memories.repository.inventory() == []
    # This memory arrived after submission, so a frozen weekly comparison cannot see it.
    target = write(store.memories.repository, key='seat', text='老师选座时偏好热闹的位置')

    async def finish(system, messages, tools, *, turn_id, workflow):
        rendered = messages[1]['content'][0]['text']
        assert receipt['operation_id'] in rendered and item['content'] in rendered
        assert '选座' in messages[0]['content']
        result = await workflow.execute_tool(ToolCall('search', 'memory_operation_search', {'query': '选座'}))
        assert target in result['candidate_ids']
        decision = {'operation_ids': ['admit'], 'action': 'noop' if action == 'reject' else action,
                    'reason': '已有覆盖' if action == 'noop' else '冲突待核实'}
        if action in ('write', 'noop'):
            decision.update(target_ids=[target], evidence=[{'event_id': receipt['operation_id'], 'quote': item['content']}])
        if action == 'write':
            decision['memory'] = {'kind': 'preference', 'key': 'seat', 'content': item['content'],
                                  'activation': 'recall', 'expires_at': None,
                                  'meta': {'tags': [], 'triggers': ['选座'], 'scope': ''}}
        result = await workflow.execute_tool(ToolCall('finish', 'memory_operation_finish', {'decisions': [decision]}))
        assert result['ok'], result
        # Planning cannot expose memory before the reviewed plan commits.
        assert store.memories.repository.inventory()[0]['content'] == '老师选座时偏好热闹的位置'
        return workflow.completion_result()

    daemon._run_agent_workflow = finish
    asyncio.run(daemon._complete_memory_operation_turn(receipt['operation_id'], asyncio.Event()))
    batch = store._db.execute('SELECT * FROM memory_operation_batches WHERE id=?', (receipt['operation_id'],)).fetchone()
    assert batch['state'] == 'completed', batch['error']
    candidate = store._db.execute('SELECT * FROM reflection_candidates WHERE id=?', (item['id'],)).fetchone()
    assert candidate['state'] == ('admitted' if action in ('write', 'noop') else 'active')
    memories = store.memories.repository.inventory()
    assert len(memories) == 1
    if action == 'write':
        assert memories[0]['id'] != target and memories[0]['meta']['triggers'] == ['选座']
    else:
        assert memories[0]['id'] == target
    if action in ('write', 'noop'):
        evidence = store.memory_evidence_for_memories([candidate['memory_id']])
        assert any(row['event_id'] == receipt['operation_id'] for row in evidence)
        records = store.memory_operation_evidence_records({row['event_id']: row['content'] for row in evidence})
        assert any(row['event_id'] == receipt['operation_id'] for row in records)
    else:
        with patch('momoi.storage.reflection.candidates.datetime') as clock:
            clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
            current = store.reflection_candidates(end='2026-10-12')[0]
            assert current['review_reason'] == '冲突待核实'
            retried = store.change_reflection_candidate(item['id'], current['revision'], admit=True)
            assert retried['operation_id'] != receipt['operation_id']


def test_pending_review_survives_restart_and_locks_weekly_updates(tmp_path):
    path = tmp_path / 'review-db'
    store = Store(path, timezone='Asia/Shanghai')
    try:
        review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
        item = store.reflection_candidates(end='2026-10-12')[0]
        with patch('momoi.storage.reflection.candidates.datetime') as clock:
            clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
            receipt = store.change_reflection_candidate(item['id'], item['revision'], admit=True)
        review(store, '2026-10-19', ['2026-10-12'], content='模型改写审核中的候选')
        pending = store.reflection_candidates(end='2026-10-19')[0]
        assert pending['content'] == item['content'] and pending['revision'] == item['revision'] + 1
        assert pending['count'] == 5
        batch = store.claim_memory_operation(receipt['operation_id'])
        store.close()
        store = Store(path, timezone='Asia/Shanghai')
        store.recover_memory_operations()
        assert store.pending_memory_operation() == receipt['operation_id']
        assert store.change_reflection_candidate(item['id'], item['revision'], admit=True) == receipt
        batch = store.claim_memory_operation(receipt['operation_id'])
        # A terminal failure leaves the candidate available for another submission.
        with store._db:
            store._db.execute('UPDATE memory_operation_batches SET failures=4 WHERE id=?', (batch['id'],))
        store.release_memory_operation(batch['id'], batch['turn_id'], 'provider failed')
        failed = store.reflection_candidates(end='2026-10-19')[0]
        assert failed['review_state'] == 'failed' and failed['review_reason'] == 'provider failed'
        assert store.memories.repository.inventory() == []
    finally:
        store.close()


def test_admission_queue_and_review_commit_roll_back_atomically(store):
    review(store, '2026-10-12', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
    item = store.reflection_candidates(end='2026-10-12')[0]
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-12'
        with pytest.raises(RuntimeError), store.transaction():
            store.change_reflection_candidate(item['id'], item['revision'], admit=True)
            raise RuntimeError('rollback submission')
        assert store.pending_memory_operation() is None
        receipt = store.change_reflection_candidate(item['id'], item['revision'], admit=True)
    batch = store.claim_memory_operation(receipt['operation_id'])
    with pytest.raises(RuntimeError), store.transaction():
        finish_admission(store, receipt, batch=batch)
        raise RuntimeError('rollback review')
    assert store.memories.repository.inventory() == []
    assert store._db.execute('SELECT state FROM reflection_candidates').fetchone()[0] == 'active'


def test_candidate_operation_migration_preserves_old_candidates(tmp_path):
    import sqlite3
    from momoi.storage.core.migrations import MIGRATIONS, _add_reflection_candidate_operation
    path = tmp_path / 'old-candidates'
    store = Store(path, timezone='Asia/Shanghai')
    review(store, '2026-10-12', ['2026-10-06', '2026-10-07'])
    store.close()
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE reflection_candidates DROP COLUMN memory_operation_id')
        db.execute(f'PRAGMA user_version={MIGRATIONS.index(_add_reflection_candidate_operation)}')
    store = Store(path, timezone='Asia/Shanghai')
    try:
        item = store.reflection_candidates(end='2026-10-12')[0]
        assert item['count'] == 2 and item['memory_operation_id'] is None
    finally:
        store.close()
    from momoi.storage.core.schema import initialize_schema
    from momoi.dashboard.backup import schema as backup_schema
    with sqlite3.connect(path) as upgraded, sqlite3.connect(':memory:') as fresh:
        initialize_schema(fresh)
        assert backup_schema(upgraded) == backup_schema(fresh)
