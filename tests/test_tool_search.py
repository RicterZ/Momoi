from momoi.models import ToolCall
from momoi.runtime.agent.runtime_tools import search_tools
from momoi.runtime.agent.tool_surface import ToolSurface


def search(query, groups, tools, limit=1):
    return search_tools(
        ToolCall('search', 'tool_search', {'query': query, 'limit': limit}),
        enable_tool_groups=groups, tools=tools, tool_surface=ToolSurface,
    )


def test_search_loads_single_tool_instead_of_whole_group_and_deduplicates():
    read = {'name': 'mcp__stock__read', 'description': '查询库存'}
    write = {'name': 'mcp__stock__write', 'description': '修改库存'}
    groups = {'stock': [read, write]}
    tools = [{'name': 'end_turn'}]
    result = search('mcp__stock__read', groups, tools)
    assert result['matched_tools'] == ['mcp__stock__read']
    assert result['newly_loaded_tools'] == ['mcp__stock__read']
    assert [t['name'] for t in tools] == ['mcp__stock__read', 'end_turn']
    assert search('库存', groups, tools)['newly_loaded_tools'] == []
    assert len(tools) == 2
    # A fresh turn does not recover discovery state.
    fresh = [{'name': 'end_turn'}]
    assert search('mcp__stock__write', groups, fresh)['newly_loaded_tools'] == ['mcp__stock__write']
    assert all(t['name'] != 'mcp__stock__read' for t in fresh)


def test_matching_ranking_no_hits_and_invalid_limits_do_not_expand_surface():
    groups = {'web': [
        {'name': 'mcp__web__find', 'description': '搜索网页'},
        {'name': 'mcp__web__other', 'description': 'mcp__web__find 的帮助'},
    ]}
    tools = []
    assert search('mcp__web__find', groups, tools)['matched_tools'] == ['mcp__web__find']
    assert search('搜索网页', groups, [])['matched_tools'] == ['mcp__web__find']
    before = list(tools)
    assert search('does-not-exist', groups, tools)['matched_tools'] == []
    for query, limit in [('', 1), ('web', 0), ('web', True), ('web', 21)]:
        assert search(query, groups, tools, limit)['ok'] is False
    assert tools == before
