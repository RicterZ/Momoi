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
        assert 'RELATIONSHIP' in str(daemon._system(planner=False))
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
    assert '<affinity>20/100</affinity>' in current_text
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
    ('{"allowed":true,"reason":"兑现了重要承诺"}', 21),
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
        captured.append(messages[0]['content'])
        return SimpleNamespace(content=[{'type': 'text', 'text': decision}], usage={}, tool_calls=[])
    provider = SimpleNamespace(complete=complete)
    request = SimpleNamespace(turn_id='review', delivery_channel=SimpleNamespace(name='dashboard'))
    call = SimpleNamespace(name='affinity_increase', arguments={'reason': '承诺兑现', 'evidence': '实际消息0'})
    try:
        result = asyncio.run(review_affinity(config, store, provider, call, request))
        assert store.self_state()['affinity'] == expected
        assert captured[0].count('USER:') == 5
        assert captured[0].count('ASSISTANT:') == 5
        assert '申请提供的证据：实际消息0' in captured[0]
        assert '当前关系阶段为：熟悉' in captured[0]
        assert 'amount' not in captured[0]
        assert 'current_affinity' not in captured[0]
        assert result['allowed'] is (expected == 21)
        captured.clear()
        invalid_call = SimpleNamespace(name=call.name, arguments={**call.arguments, 'amount': 2})
        assert not asyncio.run(review_affinity(config, store, provider, invalid_call, request))['allowed']
        assert not captured
        assert store.self_state()['affinity'] == expected
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
    call = SimpleNamespace(name='affinity_decrease', arguments={'reason': '边界被侵犯', 'evidence': '实际消息'})
    try:
        result = asyncio.run(review_affinity(config, store, SimpleNamespace(complete=complete), call, request))
        assert store.self_state()['affinity'] == 19
        assert 'value' not in result
        assert store.affinity_reviews()[0]['planner_reason'] == '边界被侵犯'
        assert store.affinity_reviews()[0]['reason'] == '持续侵犯边界'
        conflict = True
        result = asyncio.run(review_affinity(config, store, SimpleNamespace(complete=complete), call, request))
        assert not result['allowed']
        assert store.self_state()['affinity'] == 30
    finally:
        store.close()


def test_affinity_review_message_hides_numeric_state_and_history():
    from momoi.runtime.agent.affinity import affinity_review_message
    message = affinity_review_message(60, "affinity_increase", {"reason": "本次理由", "evidence": "本次证据"},
        [{"role": "user", "content": "你好"}],
        [{"allowed": True, "before": 59, "value": 60, "amount": 1, "delta": 1,
          "planner_reason": "历史理由", "reason": "审批理由", "evidence": "历史证据",
          "dialogue": [{"role": "assistant", "content": "你好我也好"}]}])
    assert not any(character.isdigit() for character in message)
    assert "当前关系阶段为：好朋友" in message
    assert "历史理由" in message and "历史证据" in message
    assert "ASSISTANT: 你好我也好" in message


def test_affinity_checker_thinking_record_links_to_planner(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from momoi.runtime.agent.affinity import review_affinity
    from momoi.llm.telemetry import persist_thinking
    from momoi.models import ToolCall
    path = tmp_path / "config.json"
    bootstrap(path)
    config = replace(ConfigurationManager(path).validate(), affinity_enabled=True)
    store = Store(config.database, config.workspace)
    store.begin_turn("review", "owner", [])
    store.record_thinking_call(created_at=1, turn_id="review", call_id="planner", stage="owner", round=2,
                              trace={"tool_calls": [{"id": "affinity-call", "name": "affinity_increase", "arguments": {"reason": "理由", "evidence": "证据"}}]})
    monkeypatch.setattr(store, "replyer_dialogue_rows", lambda channel, limit: [
        {"role": "user", "content": "实际消息"} for _ in range(10)])
    async def complete(*args, **kwargs):
        text = '{"allowed":false,"reason":"普通问候不足以增加信任"}'
        persist_thinking(store.record_thinking_call, reasoning="", assistant_text=text, tools=[], model="test")
        return SimpleNamespace(content=[{"type": "text", "text": text}], usage={}, tool_calls=[])
    request = SimpleNamespace(turn_id="review", call_id="planner", round_number=2, delivery_channel=SimpleNamespace(name="dashboard"))
    call = ToolCall("affinity-call", "affinity_increase", {"reason": "理由", "evidence": "证据"})
    try:
        asyncio.run(review_affinity(config, store, SimpleNamespace(complete=complete), call, request))
        calls = store.dashboard_thinking_detail("review")["calls"]
        checker = next(item for item in calls if item["stage"] == "affinity_checker")
        assert checker["trace"]["parent_call_id"] == "planner"
        assert checker["trace"]["tool_call_id"] == "affinity-call"
        assert checker["round"] == 2
        assert '"allowed":false' in checker["assistant_text"]
        assert store.self_state()["affinity"] == 20
    finally:
        store.close()


def test_affinity_shares_mood_context_only_when_enabled(tmp_path):
    import json
    from momoi.runtime.context.presentation import heartbeat_self_state_lines
    store = Store(tmp_path / "db")
    try:
        disabled = store.self_state_context()
        assert "affinity" not in json.loads(disabled)
        assert "<affinity>" not in heartbeat_self_state_lines(disabled)
        enabled = store.self_state_context(affinity_enabled=True)
        text = heartbeat_self_state_lines(enabled)
        assert "<mood" in text and "<affinity>20/100</affinity>" in text
    finally:
        store.close()


def test_daily_affinity_limit_persists_and_excludes_settlement(tmp_path, monkeypatch):
    from momoi.runtime.agent.harness import TurnHarness
    path = tmp_path / 'db'
    store = Store(path)
    store.begin_turn('limit', 'owner', [])
    args = {'amount': 1, 'reason': '理由', 'evidence': '证据'}
    allowed = {'allowed': True, 'reason': '审批允许'}
    for value in (20, 21, 22):
        assert store.apply_affinity_review('limit', value, 1, args, allowed)['allowed']
    assert store.affinity_daily_increases() == 3
    assert not store.apply_affinity_review('limit', 23, 1, args, allowed)['allowed']
    assert store.apply_affinity_review('limit', 23, -1, args, allowed)['allowed']
    assert store.apply_affinity_review('limit', 22, 1, {**args, 'settlement': True}, allowed)['allowed']
    assert store.affinity_daily_increases() == 3
    harness = TurnHarness.for_stage('owner')
    assert harness.affinity_limit_error('affinity_increase', 3)
    assert not harness.affinity_limit_error('affinity_decrease', 3)
    assert not TurnHarness.for_stage('reflection').affinity_limit_error('affinity_increase', 3)
    store.close()
    store = Store(path)
    assert store.affinity_daily_increases() == 3
    assert store.self_state()['affinity'] == 23
    import time
    now = time.time()
    monkeypatch.setattr('momoi.storage.agenda.heartbeat_state.time.time', lambda: now + 86400)
    assert store.affinity_daily_increases() == 0
    store.close()
