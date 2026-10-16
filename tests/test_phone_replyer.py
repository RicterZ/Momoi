import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime.agent.phone_replyer import PhoneReplyer
from tests.test_episode_annealing import config


@pytest.mark.parametrize('output', ['你好', '[WAIT]', 'tool', ''])
def test_phone_prompt_and_tool_free_completion(tmp_path, output):
    cfg = config(str(tmp_path))
    store = Mock()
    store.always_memory_context.return_value = '常驻记忆'
    store.scoped_memory_context.return_value = '当前状态'
    store.self_state_context.return_value = '心情'
    store.replyer_dialogue_rows.return_value = [
        {'turn_id': 'old', 'role': 'user', 'content': '之前的话'},
        {'turn_id': 'old', 'role': 'assistant', 'content': '之前的回应'},
        {'turn_id': 'now', 'role': 'user', 'content': '不要重复'},
    ]
    async def complete(system, messages, tools):
        assert tools == []
        assert '没有工具' in system and '[WAIT]' in system
        assert cfg.soul_prompt in system
        assert [m['content'] for m in messages[1:]] == ['之前的话', '之前的回应', '现在的话']
        assert '常驻记忆' in str(messages[0]) and '当前状态' in str(messages[0])
        return ProviderResponse([{'type': 'text', 'text': output}],
                                [ToolCall('x', 'qq_poke', {})] if output == 'tool' else [])
    replyer = PhoneReplyer(cfg, store, SimpleNamespace(complete=complete))
    event = IncomingMessage('e', 'm', '现在的话', 1, 1, channel='qq_call')
    if output in {'tool', ''}:
        with pytest.raises(ValueError):
            asyncio.run(replyer.generate([event], 'now'))
    else:
        assert asyncio.run(replyer.generate([event], 'now')) == ('' if output == '[WAIT]' else output)
    store.record_turn_usage.assert_called_once()


def test_phone_history_budget_keeps_recent_whole_messages(tmp_path):
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.replyer_dialogue_rows.return_value = [
        {'turn_id': 'old', 'role': 'user', 'content': '古' * 30000},
        {'turn_id': 'recent', 'role': 'assistant', 'content': '最近'},
    ]
    messages = PhoneReplyer(config(str(tmp_path)), store, None).messages([], 'now')
    assert [m['content'] for m in messages[1:]] == ['最近']


@pytest.mark.parametrize('output,stale', [('你好', False), ('[WAIT]', False), ('你好', True)])
def test_phone_executor_delivery_and_archive(tmp_path, output, stale):
    from unittest.mock import AsyncMock
    from momoi.runtime import MomoiDaemon
    from momoi.channel.napcat.voice_call.channel import QQCallChannel
    daemon = MomoiDaemon(config(str(tmp_path)))
    channel = QQCallChannel(SimpleNamespace(), '123', object(), tts_enabled=True)
    channel.session_id = 'call'
    channel.status = {'phase': 'connected'}
    event = IncomingMessage('e', 'm', '你好', 1, 1, channel='qq_call',
                            delivery_context=channel.routing_context())
    daemon.store.add_event(event)
    daemon.store.begin_turn('now', 'owner', ['e'])
    async def complete(system, messages, tools):
        assert tools == []
        if stale:
            channel.generation += 1
        return ProviderResponse([{'type': 'text', 'text': output}], [])
    daemon.provider = SimpleNamespace(complete=complete)
    daemon.bubble_delivery.send_voice = AsyncMock(return_value=SimpleNamespace(result={'ok': True}))
    daemon.bubble_delivery.wait_reply = AsyncMock(return_value={'ok': True})
    try:
        if stale:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(daemon._complete_batch([event], 'now', channel))
            daemon.bubble_delivery.send_voice.assert_not_called()
        else:
            asyncio.run(daemon._complete_batch([event], 'now', channel))
            assert daemon.store._db.execute("SELECT state FROM turns WHERE id='now'").fetchone()[0] == 'completed'
            exchanges = daemon.store.turn_exchanges(['now'])
            assert 'now' in exchanges
            if output == '[WAIT]':
                daemon.bubble_delivery.send_voice.assert_not_called()
            else:
                daemon.bubble_delivery.send_voice.assert_awaited_once()
                daemon.bubble_delivery.wait_reply.assert_awaited_once()
    finally:
        daemon.store.close()


def test_phone_removes_chat_actions_before_tts(tmp_path):
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.replyer_dialogue_rows.return_value = []
    async def complete(system, messages, tools):
        return ProviderResponse([{'type': 'text', 'text': '我在听呢\n\nemotion://content-sigh\nqq://poke'}], [])
    replyer = PhoneReplyer(config(str(tmp_path)), store, SimpleNamespace(complete=complete))
    assert asyncio.run(replyer.generate([], 'now')) == '我在听呢'


@pytest.mark.parametrize('effort', [None, '', 'off', 'high'])
def test_phone_runtime_thinking_override(tmp_path, effort):
    from dataclasses import replace
    from momoi.integrations.request_context import requested_thinking_effort
    cfg = replace(config(str(tmp_path)), thinking_stages={
        'replyer': 'high', **({'phone_replyer': effort} if effort is not None else {}),
    })
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.replyer_dialogue_rows.return_value = []
    async def complete(system, messages, tools):
        assert requested_thinking_effort('medium') == ('off' if effort is None else effort or 'medium')
        return ProviderResponse([{'type': 'text', 'text': '你好'}], [])
    asyncio.run(PhoneReplyer(cfg, store, SimpleNamespace(complete=complete)).generate([], 'now'))


def test_phone_prompt_dashboard_edit_is_used_next_request(tmp_path):
    from dataclasses import replace
    from momoi.dashboard.settings import DashboardSettings
    cfg = replace(config(str(tmp_path)), soul_prompt_path=tmp_path / 'SOUL.md',
                  heartbeat_prompt_path=tmp_path / 'HEARTBEAT.md')
    settings = DashboardSettings.from_config(cfg)
    assert settings.read('phone_replyer')['filename'] == 'PHONE_REPLYER.md'
    assert 'QQ 电话' in settings.read('phone_replyer')['content']
    settings.update('phone_replyer', '电话自定义表达要求')
    assert (tmp_path / 'PHONE_REPLYER.md').read_text() == '电话自定义表达要求'
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.replyer_dialogue_rows.return_value = []
    async def complete(system, messages, tools):
        assert '电话自定义表达要求' in system
        return ProviderResponse([{'type': 'text', 'text': '你好'}], [])
    asyncio.run(PhoneReplyer(cfg, store, SimpleNamespace(complete=complete)).generate([], 'now'))
    with pytest.raises(ValueError):
        settings.update('phone_replyer', '')
