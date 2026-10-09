import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from momoi.runtime import MomoiDaemon
from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
from momoi.runtime.context.retrieval import select_plan_recall_queries, build_plan_retrieval
from momoi.runtime.context.memory_search import filtered_candidates
from momoi.runtime.context.selection import RecallSelection
from momoi.models import IncomingMessage
from tests.test_recall_episode_binding import config
from tests.test_memory_tool_filters import seed


def unit(tags, scope=''):
    return {'intent': '饮食和健康习惯',
            'recall_queries': [{'semantic': '无糖咖啡', 'keywords': ['咖啡']}],
            'memory_filters': {'tags_any': tags, 'scope': scope}}


@pytest.fixture
def daemon(tmp_path):
    d = MomoiDaemon(config(str(tmp_path)))
    yield d
    d.store.close()


def plan(d, units):
    return d._plan_from_submission([], units, turn_id='test', revision=1)


def test_schema_and_plan_keep_filters_and_examples_are_direct(daemon):
    validator = Draft202012Validator(RECALL_TOOL_SPEC['input_schema'])
    assert 'examples' not in RECALL_TOOL_SPEC['input_schema']
    p = plan(daemon, {'semantic': ['饮食习惯', '运动习惯'], 'keyword': ['咖啡'],
                     'tags': ['health', 'food_drink'], 'kind': ['profile'], 'scope': 'webhook'})
    selected, *_ = select_plan_recall_queries(p)
    assert len(selected) == 2
    assert all(q['memory_filters'] == {'tags_any': ['health', 'food_drink'], 'kinds': ['profile'], 'scope': 'webhook'} for q in selected)
    assert all(q['expression'] == '咖啡' for q in selected)
    assert selected[0]['unit_ids'] == ['u1']
    assert selected[1]['unit_ids'] == ['u2']


def test_same_query_in_different_scopes_filters_before_dense_top_k(daemon):
    memory = daemon.memory
    global_id = seed(memory, 'coffee', ['health'])
    scoped_id = seed(memory, 'coffee', ['finance'], scope='webhook')
    seed(memory, 'other', ['technology'])
    dense = AsyncMock(return_value=None)
    memory.recall.dense_recall = dense
    selected, *_ = select_plan_recall_queries({'intent_units': [dict(unit(['health']), id='u1'), dict(unit(['finance'], 'webhook'), id='u2')]})
    rows = asyncio.run(filtered_candidates(memory, selected, None))
    assert {r['id']: r['unit_ids'] for r in rows} == {global_id: ['u1'], scoped_id: ['u2']}
    assert [call.kwargs['eligible_ids'] for call in dense.await_args_list] == [
        {'无糖咖啡': frozenset({str(global_id)})}, {'无糖咖啡': frozenset({str(scoped_id)})},
    ]


def test_joint_selection_receives_filtered_candidates_once(daemon):
    wanted = seed(daemon.memory, 'food', ['food_drink'])
    seed(daemon.memory, 'noise', ['technology'])
    selected, *_ = select_plan_recall_queries(plan(daemon, {'semantic': ['无糖咖啡'], 'keyword': ['咖啡'], 'tags': ['health', 'food_drink']}))
    async def select(*args, memory_candidates, **kwargs):
        assert [r['id'] for r in memory_candidates] == [wanted]
        return RecallSelection([], memory_candidates, [])
    with patch('momoi.runtime.context.service.select_topics', new=AsyncMock(side_effect=select)) as selector:
        result = asyncio.run(daemon._select_recall_topics('饮食习惯', selected, None))
    assert [r['id'] for r in result.memories] == [wanted]
    selector.assert_awaited_once()


def test_reuse_rechecks_current_tags_and_exact_scope(daemon):
    memory = daemon.memory
    global_id = seed(memory, 'coffee', ['food_drink'])
    scoped_id = seed(memory, 'coffee', ['health'], scope='webhook')
    rows = memory.snapshots([global_id, scoped_id])
    old = {'state': 'recalled', 'plan': {'intent_units': []}, 'retrieval': {
        'effective_recall_queries': ['无糖咖啡'], 'recall_memories': list(rows.values()), 'episodes': [],
    }}
    p = {'intent_units': [{'id': 'u1', 'kind': [], 'recall_queries': [],
         'memory_filters': {'scope': 'webhook', 'tags_any': ['health']},
         'recall': {'mode': 'reuse', 'from_turn_id': 'old'}}]}
    with patch.object(daemon.store, 'context_plan', return_value=old):
        result = build_plan_retrieval(daemon.store, p, daemon.config)
        assert [r['id'] for r in result['recall_memories']] == [scoped_id]
        assert result['recall_memories'][0]['meta']['scope'] == 'webhook'
        memory.repository.update_meta(rows[scoped_id], {'tags': ['finance']})
        assert build_plan_retrieval(daemon.store, p, daemon.config)['recall_memories'] == []



def test_flat_search_arguments_reject_modes_nested_units_and_empty_queries(daemon):
    validator = Draft202012Validator(RECALL_TOOL_SPEC['input_schema'])
    minimal = {'semantic': ['无糖咖啡']}
    assert validator.is_valid(minimal)
    normalized = plan(daemon, minimal)['intent_units'][0]
    assert normalized['recall_queries'] == [{'semantic': '无糖咖啡', 'keywords': []}]
    assert 'recall_mode' not in normalized
    assert 'recall_from_turn_id' not in normalized
    for invalid in [
        {}, {'semantic': []}, {'semantic': '咖啡'}, {'semantic': ['  ']},
        *[{**minimal, 'recall_mode': mode} for mode in ('search', 'skip', 'reuse')],
        {**minimal, 'intent': '饮食'}, {'units': [unit([])]},
        {**minimal, 'tags': ['invented']}, {**minimal, 'scope': 'health'},
        {**minimal, 'kind': ['健康']},
    ]:
        assert not validator.is_valid(invalid)
        with pytest.raises(ValueError):
            plan(daemon, invalid)


@pytest.mark.parametrize('arguments', [
    {'kind': ['profile']}, {'tags': ['health']},
    {'semantic': [], 'keyword': [], 'kind': ['profile'], 'tags': ['health']},
    {'scope': 'webhook'},
])
def test_metadata_only_returns_filtered_memories_without_models(daemon, arguments):
    wanted = seed(daemon.memory, 'wanted', ['health'], kind='profile',
                  scope=arguments.get('scope', ''))
    seed(daemon.memory, 'noise', ['finance'], kind='preference')
    seed(daemon.memory, 'other-scope', ['health'], kind='profile', scope='goal:other')
    daemon.semantic_recall.prepare = AsyncMock(side_effect=AssertionError('no embeddings for metadata browsing'))
    daemon.provider = SimpleNamespace(complete=AsyncMock(side_effect=AssertionError('no reranking for metadata browsing')))
    event = IncomingMessage('metadata-event', 'owner', '查找记忆', 10, 10)
    daemon.store.add_event(event)
    daemon.store.begin_turn('metadata-turn', 'owner', [event.event_id])
    asyncio.run(daemon.submit_owner_context([event], 'metadata-turn', arguments))
    rows = daemon.store.context_plan('metadata-turn')['retrieval']['recall_memories']
    assert [row['id'] for row in rows] == [wanted]
    assert rows[0]['unit_ids'] == ['u1']
    daemon.semantic_recall.prepare.assert_not_awaited()
    daemon.provider.complete.assert_not_awaited()


def test_keyword_only_is_valid_and_retrieves_memory(daemon):
    wanted = seed(daemon.memory, 'coffee', ['health'])
    selected, *_ = select_plan_recall_queries(plan(daemon, {'keyword': ['咖啡']}))
    assert selected[0]['expression'] == '咖啡'
    rows = asyncio.run(filtered_candidates(daemon.memory, selected, None))
    assert [row['id'] for row in rows] == [wanted]


def test_metadata_browse_is_bounded_and_orders_by_importance_then_recency(daemon):
    ids = [seed(daemon.memory, f'memory-{i}', ['health']) for i in range(9)]
    with daemon.store._db:
        daemon.store._db.execute('UPDATE memories SET importance=0.9 WHERE id=?', (ids[0],))
        daemon.store._db.execute('UPDATE memories SET updated_at=200 WHERE id=?', (ids[1],))
    selected, *_ = select_plan_recall_queries(plan(daemon, {'tags': ['health']}))
    result = asyncio.run(daemon._select_recall_topics('', selected, None))
    assert [row['id'] for row in result.memories] == [ids[0], ids[1], ids[8], ids[7], ids[6], ids[5]][:daemon.config.memory_results]


def test_heartbeat_metadata_only_uses_same_filters_without_embedding(daemon):
    wanted = seed(daemon.memory, 'wanted', ['health'])
    seed(daemon.memory, 'noise', ['finance'])
    daemon.semantic_recall.prepare = AsyncMock(side_effect=AssertionError('unexpected embedding'))
    result = asyncio.run(daemon.prepare_heartbeat_context({
        'activity': '记忆筛选', 'mode': 'work', 'strategy': ['查看记忆'],
        'recall_mode': 'search', 'recall_queries': [{'semantic': '', 'keywords': []}],
        'memory_filters': {'tags_any': ['health']},
    }))
    assert set(result['memory_snapshots']) == {wanted}
    daemon.semantic_recall.prepare.assert_not_awaited()
