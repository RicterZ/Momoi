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
    assert '- web: 网页搜索与浏览' in current.tool_index('worker')
    assert 'Search webpages' not in current.tool_index('worker')
    spec = next(spec for spec in current.worker_specs() if spec['name'] == 'tool_search')
    assert '网页搜索与浏览' not in spec['description']


def test_planner_surface_is_resident_without_tool_enable(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        current = surface({}, store=store)
        tools = current.conversation_specs()
        names = {spec['name'] for spec in tools}
        assert {'memory_search', 'thinking_search', 'thinking_read', 'episode_relations'} <= names
        assert not {'tool_search', 'tool_enable'} & names
        assert current.discovery_groups() == {}
        assert current.conversation_specs() == tools
        for stage in ('owner', 'heartbeat', 'webhook', 'goal'):
            assert 'thinking_read' in current.permitted_names(stage)
            assert 'tool_enable' not in current.permitted_names(stage)
    finally:
        store.close()


def test_tool_discovery_table_is_dropped(tmp_path):
    from momoi.storage.core.migrations import MIGRATIONS, _drop_tool_discovery
    path = tmp_path / 'db'
    store = Store(path)
    with store._db:
        store._db.execute('CREATE TABLE transcript_enabled_tools (name TEXT PRIMARY KEY, position INTEGER NOT NULL)')
        store._db.execute(f'PRAGMA user_version={MIGRATIONS.index(_drop_tool_discovery)}')
    store.close()
    store = Store(path)
    try:
        assert store._db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='transcript_enabled_tools'"
        ).fetchone() is None
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
        assert '日历查询与日程管理' not in str(before)
        assert 'Create an event' not in str(before)
        assert PromptRenderer._system(renderer) == before
        names = {spec['name'] for spec in current.conversation_specs()}
        assert 'thinking_read' in names and 'mcp__calendar__create' not in names
    finally:
        store.close()


def test_replyer_poke_not_a_planner_tool_and_qq_control_is_permitted(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        current = surface({}, store=store)
        from momoi.channel.onebot.voice_call.channel import QQCallChannel
        current.channel_names = ['onebot', QQCallChannel.name]
        tools = current.conversation_specs()
        names = [spec['name'] for spec in tools]
        assert 'qq_poke' not in names
        assert {'qq_recall_message', 'qq_call_status'} <= set(names)
        assert '戳一戳' in next(spec['description'] for spec in tools if spec['name'] == 'reply')
        for stage in ('owner', 'heartbeat', 'goal'):
            assert {'qq_recall_message', 'qq_call_status'} <= current.permitted_names(stage)
            assert 'qq_poke' not in current.permitted_names(stage)
    finally:
        store.close()
