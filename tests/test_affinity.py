from dataclasses import replace
import sqlite3

import pytest

from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import bootstrap
from momoi.dashboard.settings import DashboardSettings
from momoi.models import IncomingMessage
from momoi.storage import Store
from tests.test_current_state_context import daemon


def test_affinity_config_prompt_and_persistence(tmp_path):
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    assert not manager.validate().affinity_enabled
    manager.save_runtime({'affinity': {'enabled': True}}, manager.revision())
    config = manager.validate()
    assert config.affinity_enabled
    settings = DashboardSettings.from_config(config)
    assert settings.read('affinity')['filename'] == 'AFFINITY.md'
    settings.update('affinity', '亲近与信任')
    assert settings.read('affinity')['content'] == '亲近与信任'
    with pytest.raises(ValueError):
        settings.update('affinity', '')
    store = Store(config.database, config.workspace)
    assert store.self_state()['affinity'] == 20
    with store._db:
        store._db.execute('UPDATE self_state SET affinity=37')
    for value in (-1, 101):
        with pytest.raises(sqlite3.IntegrityError), store._db:
            store._db.execute('UPDATE self_state SET affinity=?', (value,))
    store.close()
    store = Store(config.database, config.workspace)
    try:
        assert store.dashboard_overview()['affinity']['value'] == 37
        manager.save_runtime({'affinity': {'enabled': False}}, manager.revision())
        assert not manager.validate().affinity_enabled
        assert store.self_state()['affinity'] == 37
    finally:
        store.close()


def test_affinity_planner_enabled_disabled_and_custom_prompt(daemon, tmp_path):
    soul = tmp_path / 'SOUL.md'
    soul.write_text('PERSONA')
    (tmp_path / 'AFFINITY.md').write_text('RELATIONSHIP')
    planner = tmp_path / 'PLANNER.md'
    daemon.config = replace(daemon.config, soul_prompt_path=soul, affinity_enabled=True)
    for content in ('{{AFFINITY}}\n```\n{{SOUL}}\n```', '```\n{{SOUL}}\n```'):
        planner.write_text(content)
        system = str(daemon._system(planner=True))
        assert system.index('RELATIONSHIP') > system.index('PERSONA')
        assert system.count('RELATIONSHIP') == 1
        assert '{{AFFINITY}}' not in system
        assert 'RELATIONSHIP' not in str(daemon._system(planner=False))
    event = IncomingMessage('affinity-input', 'owner', '你好', 1, 1)
    assert '<affinity>20/100</affinity>' in str(daemon._owner_update_message([event], daemon.channel, {}))
    daemon.config = replace(daemon.config, affinity_enabled=False)
    assert 'RELATIONSHIP' not in str(daemon._system(planner=True))
    assert '<affinity>' not in str(daemon._owner_update_message([event], daemon.channel, {}))


def test_affinity_value_is_in_current_planner_user_message(daemon):
    import asyncio
    from tests.test_current_state_context import ContextCaptured
    daemon.config = replace(daemon.config, affinity_enabled=True)
    event = IncomingMessage('affinity-current', 'owner', '你好', 1, 1)
    daemon.store.add_event(event)
    captured = {}

    async def capture(system, messages, tools, events, draft, **kwargs):
        captured['messages'] = messages
        raise ContextCaptured()

    daemon._run_tool_loop = capture
    with pytest.raises(ContextCaptured):
        asyncio.run(daemon._complete_batch([event], 'affinity-turn'))
    current_text = ''.join(block.get('text', '') for block in captured['messages'][-1]['content'])
    assert '<affinity>\n20/100\n</affinity>' in current_text
    assert all('<affinity>' not in str(message) for message in captured['messages'][:-1])


def test_affinity_tools_follow_switch():
    from types import SimpleNamespace
    from momoi.runtime.agent.tool_surface import ToolSurface
    enabled = False
    surface = ToolSurface(SimpleNamespace(tool_specs=[]), {}, affinity_enabled=lambda: enabled)
    names = {'affinity_increase', 'affinity_decrease'}
    assert not names & {spec['name'] for spec in surface.conversation_specs()}
    assert not names & surface.permitted_names('owner')
    enabled = True
    assert names <= {spec['name'] for spec in surface.conversation_specs()}
    assert names <= surface.permitted_names('owner')


@pytest.mark.parametrize('decision, expected', [
    ('{"allowed":true,"reason":"兑现了重要承诺"}', 22),
    ('{"allowed":false,"reason":"只有普通问候"}', 20),
    ('{"allowed":"true","reason":"错误类型"}', 20),
    ('not JSON', 20),
])
def test_affinity_review_only_writes_on_valid_approval(tmp_path, monkeypatch, decision, expected):
    import asyncio
    import json
    from types import SimpleNamespace
    from momoi.runtime.agent.affinity import review_affinity
    path = tmp_path / 'config.json'
    bootstrap(path)
    config = replace(ConfigurationManager(path).validate(), affinity_enabled=True)
    store = Store(config.database, config.workspace)
    store.begin_turn('review', 'owner', [])
    rows = [{'id': i, 'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'实际消息{i}', 'created_at': i} for i in range(10)]
    monkeypatch.setattr(store, 'replyer_dialogue_rows', lambda channel, limit: rows)
    captured = []
    async def complete(system, messages, tools, **kwargs):
        assert '{{AFFINITY}}' not in str(system)
        assert '# 好感度规则' in str(system)
        captured.append(json.loads(messages[0]['content']))
        return SimpleNamespace(content=[{'type': 'text', 'text': decision}], usage={}, tool_calls=[])
    provider = SimpleNamespace(complete=complete)
    request = SimpleNamespace(turn_id='review', delivery_channel=SimpleNamespace(name='dashboard'))
    call = SimpleNamespace(name='affinity_increase', arguments={'amount': 2, 'reason': '承诺兑现', 'evidence': '实际消息0'})
    try:
        result = asyncio.run(review_affinity(config, store, provider, call, request))
        assert store.self_state()['affinity'] == expected
        assert len(captured[0]['dialogue']) == 10
        assert {item['role'] for item in captured[0]['dialogue']} == {'user', 'assistant'}
        assert captured[0]['evidence'] == '实际消息0'
        assert result['allowed'] is (expected == 22)
        rows.pop()
        captured.clear()
        assert not asyncio.run(review_affinity(config, store, provider, call, request))['allowed']
        assert not captured
        assert not asyncio.run(review_affinity(replace(config, affinity_enabled=False), store, provider, call, request))['allowed']
        assert store.self_state()['affinity'] == expected
    finally:
        store.close()


def test_affinity_decrease_and_review_conflict(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from momoi.runtime.agent.affinity import review_affinity
    path = tmp_path / 'config.json'
    bootstrap(path)
    config = replace(ConfigurationManager(path).validate(), affinity_enabled=True)
    store = Store(config.database, config.workspace)
    store.begin_turn('review', 'owner', [])
    monkeypatch.setattr(store, 'replyer_dialogue_rows', lambda channel, limit: [
        {'id': i, 'role': 'user', 'content': '实际消息', 'created_at': i} for i in range(10)])
    conflict = False
    async def complete(*args, **kwargs):
        if conflict:
            with store._db:
                store._db.execute('UPDATE self_state SET affinity=30')
        return SimpleNamespace(content=[{'type': 'text', 'text': '{"allowed":true,"reason":"持续侵犯边界"}'}], usage={}, tool_calls=[])
    request = SimpleNamespace(turn_id='review', delivery_channel=SimpleNamespace(name='dashboard'))
    call = SimpleNamespace(name='affinity_decrease', arguments={'amount': 2, 'reason': '边界被侵犯', 'evidence': '实际消息'})
    try:
        result = asyncio.run(review_affinity(config, store, SimpleNamespace(complete=complete), call, request))
        assert result['value'] == 18
        assert store.affinity_reviews()[0]['planner_reason'] == '边界被侵犯'
        assert store.affinity_reviews()[0]['reason'] == '持续侵犯边界'
        conflict = True
        result = asyncio.run(review_affinity(config, store, SimpleNamespace(complete=complete), call, request))
        assert not result['allowed']
        assert store.self_state()['affinity'] == 30
    finally:
        store.close()
