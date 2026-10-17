import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from momoi.tools.builtin import BuiltinTools
from momoi.tools.deepseek_web import deepseek_web, deepseek_web_key
from momoi.models import ToolCall
from momoi.runtime.agent.tool_surface import ToolSurface


def test_toggle_hides_and_blocks_tools(tmp_path):
    for enabled in (True, False):
        surface = ToolSurface(SimpleNamespace(tool_specs=[]), {}, deepseek_web_enabled=enabled)
        names = {spec['name'] for spec in surface.conversation_specs()}
        assert ('search_web' in names) is enabled
        assert ('fetch_web' in names) is enabled
    tools = BuiltinTools(tmp_path, deepseek_web_enabled=False)
    assert asyncio.run(tools.execute(ToolCall('x', 'search_web', {'query': 'test'})))['error'] == 'tool_not_allowed'


@pytest.mark.parametrize('name,args,endpoint', [('search_web', {'query': 'test'}, 'search'), ('fetch_web', {'url': 'https://example.com'}, 'fetch')])
def test_api_request(name, args, endpoint):
    async def run():
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json={'results': []})
        with patch('momoi.tools.deepseek_web.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value = client
            assert (await deepseek_web(name, args, 'secret'))['ok']
        assert client.post.call_args.args == ('https://api.deepseek.com/v1/tools/' + endpoint,)
        assert client.post.call_args.kwargs['headers'] == {'Authorization': 'Bearer secret'}
    asyncio.run(run())


@pytest.mark.parametrize('name,args', [('search_web', {'query': ' '}), ('search_web', {'query': 'a', 'count': True}), ('fetch_web', {'url': 'file:///tmp/x'}), ('fetch_web', {'url': 'https://user:pass@example.com'})])
def test_validation(name, args):
    with pytest.raises(ValueError):
        asyncio.run(deepseek_web(name, args, 'secret'))


def test_no_key_and_no_third_party_key_leak():
    assert asyncio.run(deepseek_web('search_web', {'query': 'x'}, ''))['error'] == 'deepseek_api_key_not_configured'
    catalog = SimpleNamespace(enabled=lambda _: True, options_for=lambda _: {'base_url': 'https://proxy.example/v1', 'api_key': 'private'}, adapter_for=lambda _: 'openai')
    assert deepseek_web_key(catalog) == ''


def test_runtime_save_rejects_enable_without_credentials(tmp_path):
    from momoi.config.workspace import bootstrap
    from momoi.config.manager import ConfigurationManager
    from momoi.config.models import ConfigError
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    assert manager.validate().deepseek_web_enabled is False
    assert manager.snapshot()["app"]["tools"]["deepseek_web_enabled"] is False
    revision = manager.revision()
    with pytest.raises(ConfigError, match='请先配置 DeepSeek API Key'):
        manager.save_runtime({'tools': {'deepseek_web_enabled': True}}, revision)
    assert manager.revision() == revision
    manager.save_runtime({'tools': {'deepseek_web_enabled': False}}, revision)
    assert manager.validate().deepseek_web_enabled is False


def test_runtime_save_allows_enable_with_credentials(tmp_path):
    from momoi.config.workspace import bootstrap
    from momoi.config.manager import ConfigurationManager
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    with patch('momoi.tools.deepseek_web.deepseek_web_key', return_value='configured-key'):
        manager.save_runtime({'tools': {'deepseek_web_enabled': True}}, manager.revision())
    assert manager.validate().deepseek_web_enabled is True


def test_tools_page_exposes_deepseek_toggle():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'web/src/Tools.jsx').read_text(encoding='utf-8')
    assert 'setExecSelected("deepseek")' in source
    assert 'const builtinKey = deepseekSelected ? "deepseek_web_enabled" : "exec_enabled"' in source
    assert 'tools: { [builtinKey]: value }' in source


def test_mcp_edit_keeps_existing_server_position():
    from pathlib import Path
    import subprocess
    source = (Path(__file__).resolve().parents[1] / 'web/src/Tools.jsx').read_text(encoding='utf-8')
    start = source.index('      const servers = { ...document.mcpServers };')
    end = source.index('      await saveDocument', start)
    mutation = source[start:end]
    script = '''
const document = {mcpServers: {first: {}, middle: {}, last: {}}};
const selected = 'middle';
let name = 'middle';
const editing = {config: {disabled: true}};
''' + mutation + '''
if (Object.keys(servers).join(',') !== 'first,middle,last') throw Error('order changed');
if (!servers.middle.disabled) throw Error('edit lost');
'''
    subprocess.run(['node', '-e', script], check=True)
