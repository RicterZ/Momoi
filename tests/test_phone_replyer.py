import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime.agent.phone_replyer import PhoneReplyer
from tests.support import config


@pytest.mark.parametrize('output', ['你好', '[WAIT]', 'tool', ''])
def test_phone_prompt_and_tool_free_completion(tmp_path, output):
    cfg = config(str(tmp_path))
    store = Mock()
    store.always_memory_context.return_value = '常驻记忆'
    store.scoped_memory_context.return_value = '当前状态'
    store.self_state_context.return_value = '心情'
    store.phone_history_rows.return_value = [
        {'turn_id': 'old', 'role': 'user', 'content': '之前的话'},
        {'turn_id': 'old', 'role': 'assistant', 'content': '之前的回应'},
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
    daemon._phone_planner = AsyncMock(return_value={'result': '', 'draft': None})
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
    store.phone_history_rows.return_value = []
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
    store.phone_history_rows.return_value = []
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
    assert '语音通话' in settings.read('phone_replyer')['content']
    settings.update('phone_replyer', '电话自定义表达要求')
    assert (tmp_path / 'PHONE_REPLYER.md').read_text() == '电话自定义表达要求'
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.phone_history_rows.return_value = []
    async def complete(system, messages, tools):
        assert '电话自定义表达要求' in system
        return ProviderResponse([{'type': 'text', 'text': '你好'}], [])
    asyncio.run(PhoneReplyer(cfg, store, SimpleNamespace(complete=complete)).generate([], 'now'))
    with pytest.raises(ValueError):
        settings.update('phone_replyer', '')


@pytest.mark.parametrize('fail', [False, True])
def test_phone_stream_buffers_paragraphs_and_flushes_only_on_success(tmp_path, fail):
    from momoi.llm.streaming import ProviderEvent
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.phone_history_rows.return_value = []
    sent = []
    async def stream(*args):
        yield ProviderEvent('text_delta', '好\n')
        assert sent == []
        yield ProviderEvent('text_delta', '\n我觉')
        assert sent == ['好']
        yield ProviderEvent('reasoning_delta', 'not spoken')
        yield ProviderEvent('text_delta', '得可以\n\n最后一句')
        assert sent == ['好', '我觉得可以']
        if fail:
            raise RuntimeError('disconnected')
        yield ProviderEvent('response', data=ProviderResponse(
            [{'type': 'text', 'text': '好\n\n我觉得可以\n\n最后一句'}], []))
    async def enqueue(text):
        sent.append(text)
    replyer = PhoneReplyer(config(str(tmp_path)), store, SimpleNamespace(stream=stream))
    if fail:
        with pytest.raises(RuntimeError):
            asyncio.run(replyer.generate([], 'now', enqueue))
        assert sent == ['好', '我觉得可以']
    else:
        asyncio.run(replyer.generate([], 'now', enqueue))
        assert sent == ['好', '我觉得可以', '最后一句']


def test_phone_end_marker_flushes_tail_and_ignores_trailing_output(tmp_path):
    from momoi.llm.streaming import ProviderEvent
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.phone_history_rows.return_value = []
    sent = []
    async def stream(*args):
        yield ProviderEvent('text_delta', '好\n\n我觉得可以[EN')
        assert sent == ['好']
        yield ProviderEvent('text_delta', 'D]不要朗读')
        assert sent == ['好', '我觉得可以']
        yield ProviderEvent('text_delta', '更多不要朗读')
        yield ProviderEvent('response', data=ProviderResponse(
            [{'type': 'text', 'text': '好\n\n我觉得可以[END]不要朗读'}], []))
    async def enqueue(text):
        sent.append(text)
    result = asyncio.run(PhoneReplyer(config(str(tmp_path)), store, SimpleNamespace(stream=stream)).generate([], 'now', enqueue))
    assert result == '好\n\n我觉得可以'
    assert sent == ['好', '我觉得可以']


def test_phone_stream_timeout_does_not_speak_incomplete_tail(tmp_path, monkeypatch):
    from momoi.llm.streaming import ProviderEvent
    import momoi.runtime.agent.phone_replyer as module
    original = asyncio.wait_for
    async def short_timeout(awaitable, timeout):
        return await original(awaitable, .01)
    monkeypatch.setattr(module.asyncio, 'wait_for', short_timeout)
    store = Mock()
    store.always_memory_context.return_value = ''
    store.scoped_memory_context.return_value = ''
    store.self_state_context.return_value = ''
    store.phone_history_rows.return_value = []
    sent = []
    async def stream(*args):
        yield ProviderEvent('text_delta', '好\n\n我觉')
        await asyncio.sleep(60)
    async def enqueue(text):
        sent.append(text)
    with pytest.raises(ValueError, match='idle timeout'):
        asyncio.run(PhoneReplyer(config(str(tmp_path)), store, SimpleNamespace(stream=stream)).generate([], 'now', enqueue))
    assert sent == ['好']


def test_phone_turn_window_rotates_in_blocks_and_preserves_pairs():
    from momoi.storage.conversation.replyer import select_phone_turns
    def rows(n):
        return [{'turn_id': str(i), 'role': role, 'content': str(i), 'id': i * 2 + j}
                for i in range(n) for j, role in enumerate(('user', 'assistant'))]
    selected = select_phone_turns(rows(8), [])
    for n in range(9, 17):
        selected = select_phone_turns(rows(n), selected)
        assert len(selected) == n * 2
    selected = select_phone_turns(rows(17), selected)
    assert len(selected) == 18
    assert selected[0]['turn_id'] == '8'
    assert [r['role'] for r in selected] == ['user', 'assistant'] * 9
    huge = rows(1)
    huge[0]['content'] = '古' * 30000
    assert select_phone_turns(huge, []) == []


def test_phone_sidecar_reports_results_without_direct_speech(tmp_path):
    from unittest.mock import AsyncMock
    from momoi.runtime import MomoiDaemon
    from momoi.models import ToolCall
    daemon = MomoiDaemon(config(str(tmp_path)))
    channel = SimpleNamespace(name='qq_call')
    event = IncomingMessage('e', 'm', '查一下', 1, 1, channel='qq_call')
    async def loop(system, messages, tools, events, draft, **kwargs):
        names = {tool['name'] for tool in tools}
        assert 'phone_result' in names
        assert not names.intersection({'reply', 'wait', 'end_turn'})
        workflow = kwargs['workflow']
        assert not workflow.is_complete()
        await workflow.execute_tool(ToolCall('done', 'phone_result', {'result': '查询失败，服务不可用'}))
        assert workflow.is_complete()
    daemon._run_tool_loop = loop
    daemon._render_batch = lambda _: '查一下'
    try:
        result = asyncio.run(daemon._phone_planner([event], 'now', channel))
        assert result['result'] == '查询失败，服务不可用'
    finally:
        daemon.store.close()


def test_phone_sidecar_reaction_does_not_wait_for_planner(tmp_path):
    from momoi.runtime import MomoiDaemon
    from momoi.models import TurnDraft
    from unittest.mock import AsyncMock
    daemon = MomoiDaemon(config(str(tmp_path)))
    async def run():
        reacted = asyncio.Event()
        channel = SimpleNamespace(name='qq_call', context_valid=lambda _: True)
        event = IncomingMessage('e', 'm', '帮我查询', 1, 1, channel='qq_call')
        daemon.store.add_event(event)
        daemon.store.begin_turn('now', 'owner', ['e'])
        async def planner(*args):
            await asyncio.wait_for(reacted.wait(), 1)
            return {'result': '查询成功：结果为42', 'draft': TurnDraft()}
        calls = []
        async def complete(system, messages, tools):
            assert tools == []
            calls.append(str(messages))
            if len(calls) == 1:
                reacted.set()
                text = '我看看'
            else:
                assert '查询成功：结果为42' in str(messages)
                text = '结果是42'
            return ProviderResponse([{'type': 'text', 'text': text}], [])
        daemon.provider = SimpleNamespace(complete=complete)
        daemon._phone_planner = planner
        daemon.bubble_delivery.send_voice = AsyncMock(return_value=SimpleNamespace(result={'ok': True}))
        daemon.bubble_delivery.wait_reply = AsyncMock(return_value={'ok': True})
        await daemon._complete_phone_batch([event], 'now', channel)
        assert len(calls) == 2
        assert [call.args[0] for call in daemon.bubble_delivery.send_voice.await_args_list] == ['我看看', '结果是42']
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()
