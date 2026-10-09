import asyncio
from unittest.mock import AsyncMock

import pytest

from momoi.memory import Memory
from momoi.models import ToolCall, TurnDraft
from momoi.storage.memory.catalog import MOMOI_MEMORY_TAGS
from momoi.tools.memory import MemoryTools
from momoi.tools.contracts.memory import MEMORY_TOOL_SPECS
from momoi.runtime.workflows.memory_operation.contracts import MEMORY_OPERATION_FINISH_SPEC
from momoi.runtime.workflows.reflection_retrieval import ReflectionRetrieval
from tests.test_memory_repository import database


def seed(memory, key, tags, *, scope='', kind='preference'):
    source = {'event_id': 'e', 'quote': '无糖咖啡'}
    return memory.repository.write(
        {'kind': kind, 'key': key, 'content': '无糖咖啡',
         'activation': 'scoped' if scope else 'recall', 'expires_at': None,
         'meta': {'tags': tags, 'scope': scope}}, [], source, [source], now=100,
    )


@pytest.mark.parametrize('asynchronous', [False, True])
def test_tool_filters_union_topics_intersect_kind_and_isolate_scope(database, asynchronous):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    food = seed(memory, 'food', ['food_drink'])
    health = seed(memory, 'health', ['health'])
    seed(memory, 'finance', ['finance'])
    seed(memory, 'untagged', [])
    seed(memory, 'practice', ['health'], kind='practice')
    scoped = seed(memory, 'scoped', ['food_drink'], scope='webhook')
    tools = MemoryTools(None, memory=memory)
    def search(filters):
        draft = TurnDraft()
        call = ToolCall('search', 'memory_search', {'query': '无糖咖啡', 'filters': filters})
        result = asyncio.run(tools.execute_async(call, [], draft)) if asynchronous else tools.execute(call, [], draft)
        assert result['ok'], result
        ids = {row['id'] for row in result['results']}
        assert set(draft.memory_context) == ids
        return ids
    assert search({'tags_any': ['health', 'food_drink'], 'kinds': ['preference']}) == {food, health}
    assert search({'tags_any': ['health', 'food_drink'], 'scope': 'webhook'}) == {scoped}
    assert scoped not in search({})
    assert search({'scope': 'goal:missing'}) == set()


@pytest.mark.parametrize('filters', [
    {'tags_any': ['invented']}, {'tags_any': ['health', 'health']},
    {'tags_any': 'health'}, {'kinds': ['habit']}, {'scope': 'food'},
    {'scope': ' webhook'}, {'unknown': []},
])
def test_invalid_tool_filters_never_expand_to_unfiltered_search(database, filters):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    memory.search = AsyncMock()
    tools = MemoryTools(None, memory=memory)
    call = ToolCall('s', 'memory_search', {'query': '饮食', 'filters': filters})
    assert not asyncio.run(tools.execute_async(call, [], TurnDraft()))['ok']
    assert not tools.execute(call, [], TurnDraft())['ok']
    memory.search.assert_not_awaited()


def test_tool_passes_filters_to_dense_eligibility_before_top_k(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    wanted = seed(memory, 'finance', ['finance'])
    seed(memory, 'health', ['health'])
    dense = AsyncMock(return_value=None)
    memory.recall.dense_recall = dense
    result = asyncio.run(MemoryTools(None, memory=memory).execute_async(
        ToolCall('s', 'memory_search', {'query': '无糖咖啡', 'filters': {'tags_any': ['finance']}}), [], TurnDraft(),
    ))
    assert result['ok']
    assert dense.await_args.kwargs['eligible_ids'] == {'无糖咖啡': frozenset({str(wanted)})}


def test_reflection_recall_only_filters_memories():
    tools = AsyncMock()
    tools.execute_async.return_value = {'ok': True}
    filters = {'tags_any': ['health', 'food_drink']}
    result = asyncio.run(ReflectionRetrieval(None, tools, 30).execute(
        ToolCall('r', 'recall', {'query': '饮食', 'filters': filters}),
    ))
    assert result['ok']
    memory, episode = [call.args[0] for call in tools.execute_async.await_args_list]
    assert memory.arguments['filters'] == filters
    assert 'filters' not in episode.arguments
    assert episode.arguments['time_range'] == {'kind': 'all'}


def test_search_and_writing_share_expanded_tag_catalog():
    search = next(s for s in MEMORY_TOOL_SPECS if s['name'] == 'memory_search')
    tags = search['input_schema']['properties']['filters']['properties']['tags_any']
    assert set(tags['items']['enum']) == set(MOMOI_MEMORY_TAGS.tags)
    assert len(MOMOI_MEMORY_TAGS.tags) == 13
    import json
    schema = json.dumps(MEMORY_OPERATION_FINISH_SPEC, ensure_ascii=False)
    for tag in ('finance', 'mental_wellbeing', 'shopping', 'home_living'):
        assert tag in schema
        assert MOMOI_MEMORY_TAGS.tags[tag] in tags['description']
        assert MOMOI_MEMORY_TAGS.validate({'tags': [tag]})['tags'] == [tag]


def test_model_visible_result_keeps_tags_scope_and_triggers():
    from momoi.tools.presentation import project_tool_result
    meta = {'tags': ['finance'], 'scope': 'webhook', 'triggers': ['预算']}
    result = project_tool_result({'ok': True, 'results': [
        {'id': 1, 'content': '预算规则', 'meta': meta, 'search_score': 1.2},
    ]}, 'memory_search')
    assert result['results'][0]['meta'] == meta
    assert 'search_score' not in result['results'][0]
