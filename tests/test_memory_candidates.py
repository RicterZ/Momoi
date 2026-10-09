"""Bounded write recall and forgotten evidence boundaries, without host services."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from momoi.memory import Memory, PlanningContext, TagCatalog
from momoi.memory.retrieval.dense import VectorMemoryEvidence
from momoi.memory.retrieval.models import DenseMemoryHit, DenseThresholds
from momoi.memory.storage.transactions import transaction
from momoi.memory.writing.candidates import check_budget
from tests.test_memory_repository import database, write
from tests.test_memory_writing import request, decision


def test_semantic_candidate_is_loaded_before_planner_and_does_not_merge_itself(database):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    identifier = write(memory.repository, text='喝咖啡时不要加糖')
    dense = AsyncMock(return_value=VectorMemoryEvidence(
        {'用户喜欢无糖咖啡': {('confirmed_memory', str(identifier)): DenseMemoryHit(str(identifier), .9)}},
        {'confirmed_memory': DenseThresholds(.4, .6, .8)},
    ))
    memory.recall.dense_recall = dense
    async def planner(ctx):
        assert set(ctx.snapshots) == {identifier}
        return {'decisions': [{'operation_ids': ['op'], 'action': 'noop', 'reason': '同一事实'}]}
    planned = asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'}, planner=planner))
    assert dense.call_args.kwargs['eligible_ids'] == {'用户喜欢无糖咖啡': frozenset({str(identifier)})}
    assert memory.apply(planned, operation_id='same')['decisions'][0]['memory_ids'] == []
    assert memory.repository.inventory()[0]['id'] == identifier


def test_private_candidates_retain_always_scoped_and_forgotten_but_search_does_not(database):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    ids = [write(memory.repository, key=f'coffee{i}', text=f'coffee {i}') for i in range(4)]
    with transaction(database):
        database.execute("UPDATE memories SET activation='always' WHERE id=?", (ids[0],))
        database.execute("UPDATE memories SET activation='scoped',scope_key='heartbeat',key='coffee' WHERE id=?", (ids[1],))
    memory.repository.forget(memory.snapshots([ids[2]])[ids[2]], {'event_id': 'forget', 'quote': '忘记'}, now=10)
    ctx = PlanningContext([request(content='coffee')], {'owner:new': '喜欢无糖咖啡'}, {})
    asyncio.run(memory.writing.candidates.collect(ctx))
    assert set(ctx.snapshots) == {ids[0], ids[1], ids[3]}
    assert set(ctx.forgotten) == {ids[2]}
    assert memory.index_source.eligible_ids() == set(map(str, ids))
    assert {row['id'] for row in asyncio.run(memory.search('coffee'))} == {ids[3]}


def test_optional_candidates_are_bounded_across_private_searches(database):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    for group in range(6):
        for i in range(10):
            write(memory.repository, key=f'group{group}.{i}', text=f'group{group} coffee {i}')
    ctx = PlanningContext([request()], {'owner:new': '喜欢无糖咖啡'}, {})
    for group in range(6):
        asyncio.run(memory.writing.candidates.collect(ctx, [f'group{group}']))
        check_budget(ctx.snapshots, ctx.forgotten)
    assert len(ctx.snapshots) == 32
    assert all('forgotten_at' not in row for row in ctx.snapshots.values())


@pytest.mark.parametrize('overflow', ['requests', 'exact_duplicates', 'tokens'])
def test_mandatory_budget_overflow_defers_without_model_or_writes(database, overflow):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    requests = [request()]
    snapshots = None
    if overflow == 'requests':
        requests = [request(str(i)) for i in range(9)]
    elif overflow == 'exact_duplicates':
        for i in range(33):
            write(memory.repository, key=f'duplicate{i}', text='用户喜欢无糖咖啡')
    else:
        ids = [write(memory.repository, key=f'long{i}', text='糖' * 2000) for i in range(10)]
        snapshots = memory.snapshots(ids)
    changes = database.total_changes
    adapter = AsyncMock()
    planned = asyncio.run(memory.plan(requests, evidence={'owner:new': '喜欢无糖咖啡'},
                                      snapshots=snapshots, planner=adapter))
    assert planned.decisions[0]['action'] == 'defer'
    assert len(planned.decisions[0]['operation_ids']) == len(requests)
    adapter.assert_not_awaited()
    assert database.total_changes == changes


@pytest.mark.parametrize('fallback', ['TimeoutError: offline', 'no_active_space', 'building_initial_space'])
def test_embedding_failure_cannot_turn_into_an_unchecked_add(database, fallback):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    write(memory.repository)
    memory.recall.dense_recall = AsyncMock(return_value=VectorMemoryEvidence({}, {}, fallback))
    adapter = AsyncMock()
    planned = asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'}, planner=adapter))
    assert planned.decisions[0]['action'] == 'defer'
    assert planned.payload()['retrieval_fallback'] == fallback
    adapter.assert_not_awaited()


@pytest.mark.parametrize('timestamp', [None, 5, 10])
def test_forgotten_exact_content_under_new_key_requires_fresh_evidence(database, timestamp):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    old = write(memory.repository, text='用户喜欢无糖咖啡')
    memory.repository.forget(memory.snapshots([old])[old], {'event_id': 'forget', 'quote': '忘记'}, now=10)
    async def planner(ctx):
        assert old in ctx.forgotten and old not in ctx.snapshots
        return {'decisions': [decision(key='new.key')]}
    with pytest.raises(ValueError, match='fresh owner evidence'):
        asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'},
                               evidence_times={} if timestamp is None else {'owner:new': timestamp}, planner=planner))
    assert memory.repository.inventory() == []


def test_fresh_readdition_and_retry_do_not_restore_old_version(database):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    old = write(memory.repository, text='用户喜欢无糖咖啡')
    memory.repository.forget(memory.snapshots([old])[old], {'event_id': 'forget', 'quote': '忘记'}, now=10)
    adapter = AsyncMock(return_value={'decisions': [decision()]})
    planned = asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'},
                                     evidence_times={'owner:new': 11}, planner=adapter))
    result = memory.apply(planned, operation_id='readd')
    new = result['decisions'][0]['memory_ids'][0]
    assert new != old and not memory.snapshots([old])
    assert memory.apply(planned, operation_id='readd') == result
    assert memory.repository.tombstone('preference', 'drink') is None


def test_apply_checks_tombstone_created_after_plan_even_without_candidate(database):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    old = write(memory.repository, text='用户喜欢无糖咖啡')
    ctx = PlanningContext([request()], {'owner:new': '喜欢无糖咖啡'}, {}, evidence_times={'owner:new': 1})
    planned = memory.writing.review(ctx, {'decisions': [decision(key='renamed')]})
    memory.repository.forget(memory.snapshots([old])[old], {'event_id': 'forget', 'quote': '忘记'}, now=10)
    with pytest.raises(ValueError, match='fresh owner evidence'):
        memory.apply(planned, operation_id='stale')
    assert database.execute('SELECT count(*) FROM memory_commits').fetchone()[0] == 0


def test_scope_is_host_selected_and_filters_before_dense_search(database):
    memory = Memory(database, tags=TagCatalog({"food_drink": "饮食"}))
    general = write(memory.repository, text='用户喜欢无糖咖啡')
    scoped = write(memory.repository, key='scoped.drink', text='用户喜欢无糖咖啡')
    with transaction(database):
        database.execute("UPDATE memories SET activation='scoped',scope_key='heartbeat' WHERE id=?", (scoped,))
    dense = AsyncMock(return_value=VectorMemoryEvidence({}, {}))
    memory.recall.dense_recall = dense
    async def planner(ctx):
        assert set(ctx.snapshots) == {general}
        return {'decisions': [decision(targets=[general])]}
    asyncio.run(memory.plan([request(scope='')], evidence={'owner:new': '喜欢无糖咖啡'}, planner=planner))
    assert dense.call_args.kwargs['eligible_ids']['用户喜欢无糖咖啡'] == frozenset({str(general)})
    ctx = PlanningContext([request()], {'owner:new': '喜欢无糖咖啡'}, memory.snapshots([general, scoped]))
    with pytest.raises(ValueError, match='across scopes'):
        memory.writing.review(ctx, {'decisions': [decision(targets=[general, scoped])]})


def test_deleted_body_does_not_remove_stable_key_forget_protection(database):
    memory = Memory(database, tags=TagCatalog({'food_drink': '饮食'}))
    old = write(memory.repository)
    memory.repository.forget(memory.snapshots([old])[old], {'event_id': 'forget', 'quote': '忘记'}, now=10)
    with transaction(database):
        database.execute('DELETE FROM memories WHERE id=?', (old,))
    ctx = PlanningContext([request()], {'owner:new': '喜欢无糖咖啡'}, {}, evidence_times={'owner:new': 1})
    planned = memory.writing.review(ctx, {'decisions': [decision()]})
    with pytest.raises(ValueError, match='fresh owner evidence'):
        memory.apply(planned, operation_id='must-not-restore')
    assert memory.repository.tombstone('preference', 'drink')


def test_forgotten_candidate_change_rejects_plan_and_preserves_tombstone(database):
    memory = Memory(database, tags=TagCatalog({'food_drink': '饮食'}))
    old = write(memory.repository, text='用户喜欢无糖咖啡')
    snapshot = memory.snapshots([old])[old]
    memory.repository.forget(snapshot, {'event_id': 'forget', 'quote': '忘记'}, now=10)
    planned = asyncio.run(memory.plan([request()], evidence={'owner:new': '喜欢无糖咖啡'},
                                     evidence_times={'owner:new': 11},
                                     planner=AsyncMock(return_value={'decisions': [decision()]})))
    memory.repository.forget(snapshot, {'event_id': 'again', 'quote': '再忘记'}, now=12)
    with pytest.raises(ValueError, match='forgotten_snapshot_changed'):
        memory.apply(planned, operation_id='stale')
    assert memory.repository.tombstone('preference', 'drink')['created_at'] == 12


def test_noop_cannot_satisfy_global_request_with_scoped_memory(database):
    memory = Memory(database)
    scoped = write(memory.repository, key='scoped.drink')
    with transaction(database):
        database.execute("UPDATE memories SET activation='scoped',scope_key='heartbeat' WHERE id=?", (scoped,))
    ctx = PlanningContext([request(scope='')], {'owner:new': '喜欢无糖咖啡'}, memory.snapshots([scoped]))
    with pytest.raises(ValueError, match='request scope'):
        memory.writing.review(ctx, {'decisions': [{
            'operation_ids': ['op'], 'action': 'noop', 'reason': '重复', 'target_ids': [scoped],
            'evidence': [{'event_id': 'owner:new', 'quote': '喜欢无糖咖啡'}],
        }]})


def test_literal_noise_cannot_crowd_out_best_semantic_candidate(database):
    memory = Memory(database)
    direct = write(memory.repository, key='drink', text='喝咖啡时不要加糖')
    for index in range(8):
        write(memory.repository, key=f'noise{index}', text=f'饮品杯子颜色偏好 {index}')
    memory.recall.dense_recall = AsyncMock(return_value=VectorMemoryEvidence(
        {'饮品': {('confirmed_memory', str(direct)): DenseMemoryHit(str(direct), .9)}},
        {'confirmed_memory': DenseThresholds(.4, .6, .8)},
    ))
    ctx = PlanningContext([request(content='饮品')], {'owner:new': '喜欢无糖咖啡'}, {})
    asyncio.run(memory.writing.candidates.collect(ctx))
    assert direct in ctx.snapshots
    assert len(ctx.snapshots) == 8


def test_full_keyword_coverage_beats_newer_partial_hits(database):
    memory = Memory(database)
    direct = write(memory.repository, key='drink', text='用户喝咖啡不要糖')
    for index in range(8):
        write(memory.repository, key=f'noise{index}', text=f'用户喜欢咖啡杯款式 {index}')
    ctx = PlanningContext([request()], {'owner:new': '喜欢无糖咖啡'}, {})
    asyncio.run(memory.writing.candidates.collect(ctx, ['咖啡 糖']))
    assert direct in ctx.snapshots


def test_optional_budget_exhaustion_is_reported_instead_of_silent_loss(database):
    memory = Memory(database, tags=TagCatalog({'food_drink': '饮食'}))
    for group in range(5):
        for index in range(8):
            write(memory.repository, key=f'group{group}.{index}', text=f'group{group} coffee {index}')
    ctx = PlanningContext([request()], {'owner:new': '喜欢无糖咖啡'}, {})
    for group in range(5):
        asyncio.run(memory.writing.candidates.collect(ctx, [f'group{group}']))
    assert ctx.retrieval_fallback == 'candidate_budget_exceeded'
    assert len(ctx.snapshots) == 32
    with pytest.raises(ValueError, match='candidate recall failed'):
        memory.writing.review(ctx, {'decisions': [decision()]})
