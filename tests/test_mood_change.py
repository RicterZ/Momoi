"""Retired mood state remains historical data, never live context."""
import asyncio
from contextlib import closing
import json
from types import SimpleNamespace

import pytest

from momoi.models import AgentReply, ProviderResponse, ToolCall
from momoi.storage import Store
from tests.test_memory_operations import event, response


@pytest.mark.parametrize('outcome', ['finish', 'cancel', 'failure'])
def test_retired_mood_tool_is_unavailable_and_preserves_history(daemon, outcome):
    source = event(daemon.store, text='今天心情不错')
    turn_id = daemon._turn_id(source.event_id)
    before = daemon.store.self_state()
    rounds = 0

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        assert 'mood_change' not in {tool['name'] for tool in tools}
        for stage in ('owner', 'heartbeat', 'webhook', 'goal'):
            assert 'mood_change' not in daemon.tool_surface.permitted_names(stage)
        if rounds == 1:
            # A stale model transcript cannot reactivate the retired write path.
            return response(ToolCall('mood', 'mood_change', {
                'state': '开心', 'intensity': 0.7, 'cause': '一起解决了问题',
            }))
        assert any(block.get('is_error') for block in messages[-1]['content'])
        if outcome == 'cancel':
            raise asyncio.CancelledError
        if outcome == 'failure':
            raise RuntimeError('test failure')
        return ProviderResponse([], [])

    daemon.provider = SimpleNamespace(complete=complete)
    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    else:
        asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    after = daemon.store.self_state()
    assert {key: value for key, value in after.items() if key.startswith('mood_')} == {
        key: value for key, value in before.items() if key.startswith('mood_')
    }
    assert rounds == 2


def test_legacy_mood_survives_commit_and_reopen_without_becoming_context(tmp_path):
    path = tmp_path / 'momoi.sqlite3'
    with closing(Store(path)) as store:
        with store._db:
            store._db.execute("UPDATE self_state SET mood_state='开心', mood_intensity=0.7, mood_cause='旧记录', mood_updated_at=1 WHERE id=1")
        store.begin_turn('historical', 'owner', [])
        payload = {'mood_change': {'state': '开心', 'intensity': 0.7, 'cause': '旧记录'}}
        store.append_turn_journal('historical', 'final', payload)
        store.commit_turn([], '', AgentReply([]), turn_id='new')
    with closing(Store(path)) as store:
        assert store.self_state()['mood_state'] == '开心'
        assert store.self_state()['mood_updated_at'] == 1
        assert 'mood' not in json.loads(store.self_state_context())
        assert 'mood' not in store.dashboard_overview()
        row = store._db.execute("SELECT payload_json FROM turn_journal WHERE turn_id='historical' AND item_type='final'").fetchone()
        assert json.loads(row[0]) == payload
