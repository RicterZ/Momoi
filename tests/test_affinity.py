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
    assert store.self_state()['affinity'] == 10
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
        assert system.index('RELATIONSHIP') < system.index('PERSONA')
        assert '{{AFFINITY}}' not in system
        assert 'RELATIONSHIP' not in str(daemon._system(planner=False))
    event = IncomingMessage('affinity-input', 'owner', '你好', 1, 1)
    assert '<affinity>10/100</affinity>' in str(daemon._owner_update_message([event], daemon.channel, {}))
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
    assert '<affinity>\n10/100\n</affinity>' in current_text
    assert all('<affinity>' not in str(message) for message in captured['messages'][:-1])
