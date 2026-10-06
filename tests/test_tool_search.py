from types import SimpleNamespace

from momoi.models import ToolCall
from momoi.runtime.agent.runtime_tools import search_tools, enable_tools
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.storage import Store


def surface(groups, descriptions=None, store=None):
    catalog = [spec for specs in groups.values() for spec in specs]
    return ToolSurface(SimpleNamespace(
        tool_specs=catalog, configs={name: {'description': text} for name, text in (descriptions or {}).items()},
        tool_group=lambda name: next(group for group, specs in groups.items() if any(spec['name'] == name for spec in specs)),
    ), {}, store=store)


def search(query, groups, current, limit=1):
    return search_tools(ToolCall('search', 'tool_search', {'query': query, 'limit': limit}),
                        enable_tool_groups=groups, tool_surface=current)


def test_search_returns_candidates_without_loading_and_enable_is_exact_atomic():
    read = {'name': 'mcp__stock__read', 'description': 'Read inventory'}
    write = {'name': 'mcp__stock__write', 'description': 'Update inventory'}
    groups = {'stock': [read, write]}
    current = surface(groups, {'stock': '查询和修改库存'})
    tools = [{'name': 'end_turn'}]
    found = search('库存', groups, current, limit=2)
    assert found['tools'] == [read, write]
    assert tools == [{'name': 'end_turn'}]
    enable = lambda names: enable_tools(ToolCall('enable', 'tool_enable', {'tools': names}),
        enable_tool_groups=groups, tools=tools, tool_surface=current)
    assert enable([read['name'], 'unknown'])['error'] == 'unknown_tools'
    assert tools == [{'name': 'end_turn'}]
    assert enable([read['name'], write['name'], read['name']])['newly_loaded_tools'] == [read['name'], write['name']]
    assert enable([read['name']])['newly_loaded_tools'] == []
    assert [tool['name'] for tool in tools] == [read['name'], write['name'], 'end_turn']
    for names in [[], '', [None], [''], ['x'] * 21]:
        assert enable(names)['error'] == 'invalid_tool_enable'


def test_search_ranking_limits_and_short_system_index():
    groups = {'web': [
        {'name': 'mcp__web__find', 'description': 'Search webpages'},
        {'name': 'mcp__web__other', 'description': 'Help for mcp__web__find'},
    ]}
    current = surface(groups, {'web': '网页搜索与浏览'})
    assert search('mcp__web__find', groups, current)['tools'][0]['name'] == 'mcp__web__find'
    result = search('网页', groups, current)
    assert result['total'] == 2 and result['has_more']
    assert search('missing', groups, current)['tools'] == []
    for query, limit in [('', 1), ('web', 0), ('web', True), ('web', 21)]:
        assert not search(query, groups, current, limit)['ok']
    assert '- web: 网页搜索与浏览' in current.tool_index()
    assert 'Search webpages' not in current.tool_index()
    spec = next(spec for spec in current.conversation_specs() if spec['name'] == 'tool_search')
    assert '网页搜索与浏览' not in spec['description']


def test_enabled_schema_persists_across_surfaces_and_restart(tmp_path):
    groups = {'web': [{'name': 'mcp__web__find', 'description': 'Search webpages'}]}
    path = tmp_path / 'db'
    store = Store(path)
    current = surface(groups, store=store)
    tools = current.conversation_specs()
    enable_tools(ToolCall('enable', 'tool_enable', {'tools': ['mcp__web__find']}),
                 enable_tool_groups=groups, tools=tools, tool_surface=current)
    assert current.conversation_specs() == tools
    for stage in ('owner', 'heartbeat', 'goal'):
        assert 'tool_enable' in current.permitted_names(stage)
    assert 'tool_enable' not in current.permitted_names('webhook')
    store.close()
    store = Store(path)
    try:
        assert surface(groups, store=store).conversation_specs() == tools
    finally:
        store.close()


def test_compaction_clears_enabled_tools_only_at_boundary(tmp_path):
    from momoi.models import IncomingMessage, AgentReply
    store = Store(tmp_path / 'db')
    try:
        def commit(n):
            event = IncomingMessage(str(n), '1', '测试消息', n, n)
            store.add_event(event)
            store.commit_turn([event], event.text, AgentReply([]), turn_id=str(n))
        commit(1)
        store.transcript_window_turn_limit(1, 3)
        store.enable_transcript_tools(['mcp__web__find'])
        commit(2)
        store.transcript_window_turn_limit(1, 3)
        assert store.transcript_enabled_tools() == ['mcp__web__find']
        commit(3)
        store.transcript_window_turn_limit(1, 3)
        assert store.transcript_enabled_tools() == []
        store.enable_transcript_tools(['mcp__web__find'])
        store.transcript_window_turn_limit(1, 3, force_compact=True)
        assert store.transcript_enabled_tools() == []
    finally:
        store.close()


def test_tool_discovery_migration_and_empty_manual_compact(tmp_path):
    from momoi.storage.core.migrations import SCHEMA_VERSION
    path = tmp_path / 'db'
    store = Store(path)
    with store._db:
        store._db.execute('DROP TABLE transcript_enabled_tools')
        store._db.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
    store.close()
    store = Store(path)
    try:
        store.enable_transcript_tools(['tool'])
        store.transcript_window_turn_limit(1, 3, force_compact=True)
        assert store.transcript_enabled_tools() == []
    finally:
        store.close()


def test_system_index_is_independent_of_enabled_tool_schema(tmp_path):
    from momoi.runtime.prompt_renderer import PromptRenderer
    groups = {'calendar': [{'name': 'mcp__calendar__create', 'description': 'Create an event'}]}
    store = Store(tmp_path / 'db')
    try:
        current = surface(groups, {'calendar': '日历查询与日程管理'}, store)
        renderer = SimpleNamespace(
            _workspace_soul=lambda: '测试身份', _contract=lambda: '测试规则',
            store=SimpleNamespace(emotion_context=lambda: ''), tool_surface=current,
        )
        before = PromptRenderer._system(renderer)
        assert '日历查询与日程管理' in before[-1]['text']
        assert 'Create an event' not in str(before)
        store.enable_transcript_tools(['mcp__calendar__create'])
        assert PromptRenderer._system(renderer) == before
        assert 'mcp__calendar__create' in {spec['name'] for spec in current.conversation_specs()}
    finally:
        store.close()
