"""Memory plans and commits work without Momoi events, turns, or model providers."""
import asyncio
from copy import deepcopy
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from momoi.memory import Memory, MemoryPlan, PlanningContext, TagCatalog
from momoi.memory.storage.transactions import transaction
from tests.test_memory_repository import database, write


def request(identifier='op', event_id='owner:new', **extra):
    return {'id': identifier, 'type': 'add', 'event_id': event_id,
            'content': '用户喜欢无糖咖啡', 'evidence': '喜欢无糖咖啡', **extra}


def decision(identifier='op', *, key='drink', targets=(), event_id='owner:new', **extra):
    return {'operation_ids': [identifier], 'action': 'write', 'reason': '新用户证据',
            'target_ids': list(targets), 'memory': {
                'kind': 'preference', 'key': key, 'content': '用户喜欢无糖咖啡',
                'activation': 'recall', 'expires_at': None, 'meta': {'tags': ['food_drink']},
            }, 'evidence': [{'event_id': event_id, 'quote': '喜欢无糖咖啡'}], **extra}


def context(memory, ids=(), requests=None):
    return PlanningContext(requests or [request()], {'owner:new': '我喜欢无糖咖啡'}, memory.snapshots(list(ids)))


@pytest.fixture
def memory(database):
    return Memory(database, tags=TagCatalog({'food_drink': '饮食'}))


def plan(memory, commands=None, ids=()):
    return memory.writing.review(context(memory, ids), {'decisions': commands or [decision()]})


def test_plan_calls_injected_adapter_without_writing_and_seals_copies(database, memory):
    original = context(memory)
    answer = {'decisions': [decision()]}
    adapter = AsyncMock(return_value=answer)
    changes = database.total_changes
    planned = asyncio.run(memory.plan(original.requests, evidence=original.evidence, planner=adapter))
    assert database.total_changes == changes
    assert not database.in_transaction
    assert database.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0
    adapter.assert_awaited_once()
    answer['decisions'][0]['memory']['content'] = 'changed'
    original.requests[0]['content'] = 'changed'
    exposed = planned.decisions
    exposed[0]['memory']['content'] = 'changed again'
    assert planned.decisions[0]['memory']['content'] == '用户喜欢无糖咖啡'
    result = memory.apply(planned, operation_id='batch:1')
    assert result['decisions'][0]['memory_ids'] == [1]
    assert memory.snapshots([1])[1]['meta'] == {'tags': ['food_drink'], 'scope': ''}
    assert adapter.await_count == 1


@pytest.mark.parametrize('change', ['request', 'evidence'])
def test_planner_cannot_rewrite_authenticated_input(memory, change):
    async def adapter(ctx):
        if change == 'request':
            ctx.requests[0]['content'] = 'different'
        else:
            ctx.evidence['owner:new'] = 'different'
        return {'decisions': [decision()]}
    with pytest.raises(ValueError, match='changed the request or evidence'):
        asyncio.run(memory.plan([request()], evidence={'owner:new': '我喜欢无糖咖啡'}, planner=adapter))


def test_host_adapter_can_add_verified_candidates_and_evidence(memory):
    target = write(memory.repository)
    async def adapter(ctx):
        ctx.snapshots.update(memory.snapshots([target]))
        ctx.evidence['event:1'] = '喜欢无糖咖啡'
        return {'decisions': [decision(targets=[target])]}
    planned = asyncio.run(memory.plan([request()], evidence={'owner:new': '我喜欢无糖咖啡'}, planner=adapter))
    result = memory.apply(planned, operation_id='replacement')
    identifier = result['decisions'][0]['memory_ids'][0]
    assert identifier != target
    assert memory.snapshots([target]) == {}
    assert memory.snapshots([identifier])[identifier]['content'] == '用户喜欢无糖咖啡'


def test_retry_survives_new_connection_and_later_target_changes(database, memory):
    target = write(memory.repository)
    reviewed = plan(memory, [decision(targets=[target])], [target])
    result = memory.apply(reviewed, operation_id='once')
    identifier = result['decisions'][0]['memory_ids'][0]
    snapshot = memory.snapshots([identifier])[identifier]
    memory.repository.update_meta(snapshot, {'tags': []})
    path = database.execute('PRAGMA database_list').fetchone()['file']
    other = sqlite3.connect(path)
    other.row_factory = sqlite3.Row
    try:
        # Receipts survive restart and return without applying the stale plan again.
        restarted = Memory(other)
        assert restarted.apply(MemoryPlan(reviewed.payload_json), operation_id='once') == result
        assert restarted.snapshots([identifier])[identifier]['meta'] == {'tags': [], 'scope': ''}
        assert other.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 1
        assert other.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 2
    finally:
        other.close()


@pytest.mark.parametrize('field', ['content', 'meta', 'quote', 'request', 'snapshot'])
def test_operation_id_binds_complete_plan(database, memory, field):
    target = write(memory.repository)
    reviewed = plan(memory, [decision(targets=[target])], [target])
    memory.apply(reviewed, operation_id='once')
    payload = reviewed.payload()
    if field == 'content':
        payload['decisions'][0]['memory']['content'] = '用户喜欢茶'
    elif field == 'meta':
        payload['decisions'][0]['memory']['meta'] = {'tags': []}
    elif field == 'quote':
        payload['decisions'][0]['evidence'][0]['quote'] = '咖啡'
    elif field == 'request':
        payload['requests'][0]['content'] = '另一个请求'
    else:
        payload['snapshots'][str(target)]['content'] = '修改旧快照'
    with pytest.raises(ValueError, match='memory_operation_id_conflict'):
        memory.apply(MemoryPlan(json.dumps(payload)), operation_id='once')
    assert database.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 2


def test_apply_receipt_and_evidence_roll_back_with_host_transaction(database, memory):
    reviewed = plan(memory)
    with pytest.raises(RuntimeError, match='host failed'):
        with transaction(database):
            database.execute("INSERT INTO application_work VALUES ('journal')")
            memory.apply(reviewed, operation_id='atomic')
            assert database.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 1
            assert database.in_transaction
            raise RuntimeError('host failed')
    for table in ('memories', 'memory_evidence', 'memory_commits', 'application_work'):
        assert database.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert memory.apply(reviewed, operation_id='atomic')['decisions'][0]['memory_ids'] == [1]


def test_failed_second_write_rolls_back_first_and_preserves_parent_work(database, memory):
    existing = write(memory.repository, key='conflict')
    ctx = context(memory, requests=[request('one'), request('two')])
    reviewed = memory.writing.review(ctx, {'decisions': [decision('one'), decision('two', key='conflict')]})
    with transaction(database):
        database.execute("INSERT INTO application_work VALUES ('keep')")
        with pytest.raises(ValueError, match='memory_key_conflict'):
            memory.apply(reviewed, operation_id='conflict')
        assert database.in_transaction
    assert database.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0
    assert [row['id'] for row in memory.repository.inventory()] == [existing]
    assert database.execute('SELECT COUNT(*) FROM application_work').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['content', 'meta', 'forgotten', 'superseded'])
def test_stale_plan_rejected_without_receipt(database, memory, change):
    target = write(memory.repository)
    reviewed = plan(memory, [decision(targets=[target])], [target])
    snapshot = memory.snapshots([target])[target]
    if change == 'content':
        with transaction(database):
            database.execute("UPDATE memories SET content='新事实' WHERE id=?", (target,))
    elif change == 'meta':
        memory.repository.update_meta(snapshot, {'tags': ['food_drink']})
    elif change == 'forgotten':
        memory.repository.forget(snapshot, {'event_id': 'forget', 'quote': 'forget'}, now=1)
    else:
        write(memory.repository, targets=[target])
    with pytest.raises(ValueError, match='memory_snapshot_changed'):
        memory.apply(reviewed, operation_id='stale')
    assert database.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0


@pytest.mark.parametrize('malformed', ['unknown_id', 'unknown_tag', 'quote', 'action', 'duplicate_request'])
def test_apply_revalidates_deserialized_model_commands(database, memory, malformed):
    payload = plan(memory).payload()
    command = payload['decisions'][0]
    if malformed == 'unknown_id':
        command['target_ids'] = [999]
    elif malformed == 'unknown_tag':
        command['memory']['meta']['tags'] = ['invented']
    elif malformed == 'quote':
        command['evidence'][0]['quote'] = 'not in original text'
    elif malformed == 'action':
        command['action'] = 'DELETE ALL'
    else:
        payload['requests'].append(deepcopy(payload['requests'][0]))
    with pytest.raises(ValueError):
        memory.apply(MemoryPlan(json.dumps(payload)), operation_id='invalid')
    assert database.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0


def test_forget_noop_defer_and_merge_share_commit_path(database, memory):
    first = write(memory.repository)
    second = write(memory.repository, key='duplicate', text='咖啡不加糖')
    merged = memory.apply(plan(memory, [decision(targets=[first, second])], [first, second]), operation_id='merge')
    identifier = merged['decisions'][0]['memory_ids'][0]
    assert identifier not in {first, second}
    assert memory.snapshots([first, second]) == {}
    assert {row[0] for row in database.execute('SELECT quote FROM memory_evidence WHERE memory_id=?', (identifier,))} == {
        '喜欢无糖咖啡', '咖啡不加糖',
    }
    for action in ('noop', 'defer'):
        reviewed = plan(memory, [{'operation_ids': ['op'], 'action': action, 'reason': '无变更'}])
        result = memory.apply(reviewed, operation_id=action)
        assert result['decisions'][0]['memory_ids'] == []
        assert memory.apply(reviewed, operation_id=action) == result
    command = {'operation_ids': ['op'], 'action': 'forget', 'reason': '忘记',
               'target_ids': [identifier], 'evidence': [{'event_id': 'owner:new', 'quote': '喜欢无糖咖啡'}]}
    # The library verifies structure/quotes, while the host/model verifies owner intent.
    reviewed = plan(memory, [command], [identifier])
    result = memory.apply(reviewed, operation_id='forget')
    assert memory.snapshots([identifier]) == {}
    assert memory.apply(reviewed, operation_id='forget') == result
    assert database.execute('SELECT COUNT(*) FROM memory_tombstones').fetchone()[0] == 1


def test_planning_requires_adapter_and_no_open_write_transaction(database, memory):
    with pytest.raises(ValueError, match='injected planner'):
        asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'}))
    adapter = AsyncMock()
    with transaction(database):
        with pytest.raises(ValueError, match='idle database'):
            asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'}, planner=adapter))
    adapter.assert_not_awaited()


def test_search_during_planning_does_not_delete_expired_rows(database, memory):
    expired = write(memory.repository)
    with transaction(database):
        database.execute('UPDATE memories SET expires_at=1 WHERE id=?', (expired,))
    changes = database.total_changes
    async def adapter(ctx):
        assert memory.search_literal('咖啡', 8, include_scoped=True) == []
        assert await memory.search('咖啡') == []
        return {'decisions': [{'operation_ids': ['op'], 'action': 'defer', 'reason': '需核实'}]}
    asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'}, planner=adapter))
    assert database.total_changes == changes
    assert database.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['missing_quote', 'unknown_source', 'bad_target', 'extra_field'])
def test_invalid_requests_do_not_reach_planner(memory, change):
    value = request()
    if change == 'missing_quote':
        del value['evidence']
    elif change == 'unknown_source':
        value['event_id'] = 'unknown'
    elif change == 'bad_target':
        value['target_id'] = True
    else:
        value['authority'] = 'owner'
    adapter = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(memory.plan([value], evidence={'owner:new': '喜欢无糖咖啡'}, planner=adapter))
    adapter.assert_not_awaited()


@pytest.mark.parametrize('error', [TimeoutError('offline'), asyncio.CancelledError()])
def test_model_failure_leaves_no_memory_or_receipt(database, memory, error):
    adapter = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'}, planner=adapter))
    assert database.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0


def test_receipt_hash_is_independent_of_json_format(database, memory):
    reviewed = plan(memory)
    result = memory.apply(reviewed, operation_id='canonical')
    serialized = json.dumps(reviewed.payload(), ensure_ascii=True, indent=4)
    assert memory.apply(MemoryPlan(serialized), operation_id='canonical') == result
    assert database.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 1


@pytest.mark.parametrize('action', ['noop', 'metadata'])
def test_evidence_only_and_metadata_plans_preserve_content_version(database, memory, action):
    target = write(memory.repository)
    ctx = context(memory, [target])
    command = {'operation_ids': ['op'], 'action': action, 'reason': '重复事实或修订标签',
               'target_ids': [target], 'evidence': [{'event_id': 'owner:new', 'quote': '喜欢无糖咖啡'}]}
    if action == 'metadata':
        command['meta'] = {'tags': ['food_drink']}
    before = memory.snapshots([target])[target]
    documents = memory.index_source.documents(str(target))
    reviewed = memory.writing.review(ctx, {'decisions': [command]})
    result = memory.apply(reviewed, operation_id=action)
    assert result['decisions'][0]['memory_ids'] == [target]
    after = memory.snapshots([target])[target]
    assert after['content'] == before['content']
    assert after['source_event_id'] == before['source_event_id']
    assert after['created_at'] == before['created_at']
    assert after['meta']['tags'] == (['food_drink'] if action == 'metadata' else [])
    assert database.execute('SELECT count(*) FROM memories').fetchone()[0] == 1
    assert memory.index_source.documents(str(target)) == documents
    assert {row[0] for row in database.execute('SELECT source_event_id FROM memory_evidence WHERE memory_id=?', (target,))} == {'owner:new', 'event:1'}
    assert memory.apply(reviewed, operation_id=action) == result
    assert database.execute('SELECT count(*) FROM memory_evidence').fetchone()[0] == 2


@pytest.mark.parametrize('bad', ['unknown_tag', 'stale', 'evidence', 'no_target'])
def test_metadata_plans_reject_invalid_changes_atomically(database, memory, bad):
    target = write(memory.repository)
    ctx = context(memory, [target])
    command = {'operation_ids': ['op'], 'action': 'metadata', 'reason': '标签',
               'target_ids': [target], 'meta': {'tags': ['food_drink']},
               'evidence': [{'event_id': 'owner:new', 'quote': '喜欢无糖咖啡'}]}
    if bad == 'unknown_tag':
        command['meta']['tags'] = ['invented']
    elif bad == 'evidence':
        command['evidence'][0]['quote'] = '不存在的引用'
    elif bad == 'no_target':
        command['target_ids'] = []
    if bad == 'stale':
        planned = memory.writing.review(ctx, {'decisions': [command]})
        memory.repository.update_meta(ctx.snapshots[target], {'tags': ['food_drink']})
        with pytest.raises(ValueError, match='snapshot_changed'):
            memory.apply(planned, operation_id='bad')
    else:
        with pytest.raises(ValueError):
            memory.writing.review(ctx, {'decisions': [command]})
    assert database.execute('SELECT count(*) FROM memory_commits').fetchone()[0] == 0
    assert database.execute('SELECT count(*) FROM memory_evidence').fetchone()[0] == 1
