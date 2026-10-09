import asyncio
from types import SimpleNamespace

import pytest

from momoi.models import ProviderResponse, ToolCall
from tests.test_memory_operations import daemon, event, response


@pytest.mark.parametrize('outcome', ['finish', 'cancel', 'failure'])
def test_mood_change_is_committed_only_on_normal_completion(daemon, outcome):
    source = event(daemon.store, text='今天心情不错')
    turn_id = daemon._turn_id(source.event_id)
    before = daemon.store.self_state()
    rounds = 0

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        names = {t['name'] for t in tools}
        assert 'mood_change' in names and 'end_turn' not in names
        assert daemon.store.self_state()['mood_updated_at'] == before['mood_updated_at']
        if rounds == 1:
            return response(ToolCall('mood', 'mood_change', {
                'state': 'happy', 'intensity': 0.7, 'cause': '一起解决了问题',
            }))
        assert 'staged' in str(messages[-1])
        if outcome == 'cancel':
            raise asyncio.CancelledError
        if outcome == 'failure':
            raise RuntimeError('test failure')
        return ProviderResponse([{'type': 'text', 'text': '本轮完成'}], [])

    daemon.provider = SimpleNamespace(complete=complete)
    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    else:
        asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    after = daemon.store.self_state()
    if outcome == 'finish':
        assert after['mood_state'] == 'happy'
        assert after['mood_intensity'] == 0.7
        assert after['mood_cause'] == '一起解决了问题'
        assert not daemon.store.due_outbox()
    else:
        assert after['mood_updated_at'] == before['mood_updated_at']
    assert rounds == 2


def test_invalid_mood_does_not_replace_staged_update(daemon):
    source = event(daemon.store)
    calls = [
        ToolCall('valid', 'mood_change', {'state': 'calm', 'intensity': 0.2, 'cause': '安静下来'}),
        ToolCall('invalid', 'mood_change', {'state': 'happy', 'intensity': 2, 'cause': '错误强度'}),
    ]

    async def complete(system, messages, tools, **kwargs):
        if calls:
            return response(calls.pop(0))
        assert 'error' in str(messages[-1])
        return ProviderResponse([], [])

    daemon.provider = SimpleNamespace(complete=complete)
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), daemon._turn_id(source.event_id)))
    after = daemon.store.self_state()
    assert after['mood_state'] == 'calm'
    assert after['mood_intensity'] == 0.2
    assert after['mood_cause'] == '安静下来'


def test_unchanged_mood_needs_no_tool_call(daemon):
    source = event(daemon.store)
    before = daemon.store.self_state()

    async def complete(*args, **kwargs):
        return ProviderResponse([], [])

    daemon.provider = SimpleNamespace(complete=complete)
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), daemon._turn_id(source.event_id)))
    assert daemon.store.self_state()['mood_updated_at'] == before['mood_updated_at']
    assert not daemon.store.due_outbox()
