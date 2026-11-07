from momoi.memory import Memory
from momoi.models import ToolCall, TurnDraft
from momoi.storage.memory.catalog import MOMOI_MEMORY_TAGS
from momoi.storage.memory.context import MEMORY_CATALOG_INLINE_LIMIT, memory_catalog_text
from momoi.tools.memory import MEMORY_BROWSE_PAGE, MemoryTools


def seed(memory, key, tags, *, scope='', activation=None, content='无糖咖啡'):
    source = {'event_id': 'e', 'quote': content}
    return memory.repository.write(
        {'kind': 'preference', 'key': key, 'content': content,
         'activation': activation or ('scoped' if scope else 'recall'), 'expires_at': None,
         'meta': {'tags': tags, 'scope': scope}}, [], source, [source], now=100,
    )


def forget(memory, memory_id):
    memory.repository.forget(memory.snapshots([memory_id])[memory_id], {'event_id': 'e', 'quote': '忘掉'}, now=200)


def call(tools, name, arguments, draft=None):
    return tools.execute(ToolCall('c', name, arguments), [], draft or TurnDraft())


def test_catalog_counts_recallable_memory_and_inlines_small_topics(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    leisure = [seed(memory, f'game {index}', ['leisure']) for index in range(MEMORY_CATALOG_INLINE_LIMIT + 1)]
    food = seed(memory, '不吃香菜', ['food_drink', 'health'])
    untagged = seed(memory, '称呼', [])
    seed(memory, 'always', ['food_drink'], activation='always')
    seed(memory, 'webhook only', ['food_drink'], scope='webhook')
    deleted = seed(memory, 'deleted', ['food_drink'])
    forget(memory, deleted)

    text = memory_catalog_text(memory.repository)
    assert f'leisure（{MOMOI_MEMORY_TAGS.tags["leisure"]}） {len(leisure)} 条' in text
    assert f'health（{MOMOI_MEMORY_TAGS.tags["health"]}）：#{food} 不吃香菜' in text
    assert text.splitlines()[-1] == f'untagged（未标主题）：#{untagged} 称呼'
    assert 'always' not in text and 'webhook only' not in text and f'#{deleted}' not in text
    assert memory_catalog_text(memory.repository) == text
    assert 'webhook only' in memory_catalog_text(memory.repository, scope='webhook')


def test_catalog_is_empty_without_recallable_memory(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    seed(memory, 'always', ['food_drink'], activation='always')
    assert memory_catalog_text(memory.repository) == ''


def test_browse_pages_topic_without_query_and_matches_catalog_buckets(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    ids = [seed(memory, f'food {index}', ['food_drink'], content='x' * 100) for index in range(MEMORY_BROWSE_PAGE + 2)]
    untagged = seed(memory, 'untagged', [])
    tools = MemoryTools(None, memory=memory)

    first = call(tools, 'memory_browse', {'topic': 'food_drink'})
    assert first['ok'] and first['total'] == len(ids)
    assert [item['id'] for item in first['items']] == ids[:MEMORY_BROWSE_PAGE]
    assert first['items'][0]['truncated'] and len(first['items'][0]['excerpt']) == 80
    second = call(tools, 'memory_browse', {'topic': 'food_drink', 'cursor': first['next_cursor']})
    assert [item['id'] for item in second['items']] == ids[MEMORY_BROWSE_PAGE:]
    assert 'next_cursor' not in second
    assert [item['id'] for item in call(tools, 'memory_browse', {'topic': 'untagged'})['items']] == [untagged]
    assert not call(tools, 'memory_browse', {'topic': 'invented'})['ok']


def test_browse_isolates_scope(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    seed(memory, 'global', ['food_drink'])
    scoped = seed(memory, 'webhook', ['food_drink'], scope='webhook')
    tools = MemoryTools(None, memory=memory)
    assert [item['key'] for item in call(tools, 'memory_browse', {'topic': 'food_drink'})['items']] == ['global']
    result = call(tools, 'memory_browse', {'topic': 'food_drink', 'scope': 'webhook'})
    assert [item['id'] for item in result['items']] == [scoped]


def test_read_returns_live_memory_and_records_only_read_snapshots(database):
    memory = Memory(database, tags=MOMOI_MEMORY_TAGS)
    live = seed(memory, '不吃香菜', ['food_drink'], content='主人不吃香菜')
    gone = seed(memory, 'gone', ['food_drink'])
    forget(memory, gone)
    tools = MemoryTools(None, memory=memory)

    browse_draft = TurnDraft()
    call(tools, 'memory_browse', {'topic': 'food_drink'}, browse_draft)
    assert browse_draft.memory_context == {}

    draft = TurnDraft()
    result = call(tools, 'memory_read', {'ids': [live, gone]}, draft)
    assert result['ok']
    assert [item['content'] for item in result['memories']] == ['主人不吃香菜']
    assert result['memories'][0]['meta']['tags'] == ['food_drink']
    assert result['missing_ids'] == [gone]
    assert set(draft.memory_context) == {live}
    assert not call(tools, 'memory_read', {'ids': []})['ok']


def test_catalog_sits_in_context_prefix_after_goal_directory():
    from momoi.runtime.turn_support import context_data_message
    message = context_data_message(
        ('long_term_memories', 'a'), ('goal_directory', 'g'), ('memory_catalog', '#1 <x>'), required=True,
    )
    text = message['content'] if isinstance(message['content'], str) else message['content'][0]['text']
    assert text.index('<goal_directory>') < text.index('<memory_catalog>')
    assert '#1 &lt;x&gt;' in text


def test_browse_and_read_are_visible_without_tool_enable():
    from momoi.runtime.agent.tool_surface import DEFERRED_TOOLS
    assert {'memory_browse', 'memory_read'}.isdisjoint(DEFERRED_TOOLS)
