import asyncio
import json
from unittest.mock import patch

import pytest

from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from tests.test_episode_annealing import config


def response(*calls):
    return ProviderResponse([{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.arguments} for c in calls], list(calls))


@pytest.mark.parametrize('with_messages', [False, True])
def test_wait_returns_messages_in_result_and_prevents_stale_actions(tmp_path, with_messages):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        started, finish = asyncio.Event(), asyncio.Event()
        captured = []
        image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'aW1hZ2U='}}
        daemon.channel.content_blocks = lambda segments: [image] if segments else []
        async def sleep(seconds):
            assert seconds == 60
            started.set()
            await finish.wait()
        class Provider:
            async def complete(self, system, messages, tools, **kwargs):
                captured.append(json.loads(json.dumps(messages)))
                assert 'wait' in {t['name'] for t in tools}
                if len(captured) == 1:
                    return response(ToolCall('wait-1', 'wait', {'seconds': 60}),
                                    ToolCall('stale', 'read_file', {'path': 'must-not-read'}))
                assert len(captured) == 2
                return response(ToolCall('end', 'end_turn', {'mood': {'decision': 'unchanged'}}))
        daemon.provider = Provider()
        initial = IncomingMessage('first', 'first', '我有件事', 1, 1)
        daemon.store.add_event(initial)
        batch = [initial]
        with patch('momoi.runtime.workflows.owner.updates.asyncio.sleep', sleep):
            task = daemon.start_active_turn(daemon._complete_batch_turn(batch, asyncio.Event(), 'wait-turn'), stage='owner', channel=daemon.channel.name)
            await asyncio.wait_for(started.wait(), 2)
            updates = [IncomingMessage('next', 'next', '后半句话 [表情] [戳一戳]', 2, 2,
                       ({'type': 'text', 'text': 'attachment-marker'},)),
                       IncomingMessage('last', 'last', '补充完了', 3, 3)] if with_messages else []
            for event in updates:
                await daemon._receive(event)
            assert not task.done() and len(captured) == 1
            finish.set()
            await asyncio.wait_for(task, 3)
        assert batch == [initial, *updates]
        blocks = [b for m in captured[-1] if isinstance(m.get('content'), list) for b in m['content']]
        result = next(b for b in blocks if b.get('tool_use_id') == 'wait-1')
        text = result['content']
        payload = json.loads(text)
        assert len(payload['messages']) == len(updates)
        assert [m['text'] for m in payload['messages']] == [event.text for event in updates]
        assert [m['message_id'] for m in payload['messages']] == [event.message_id for event in updates]
        assert '<current_messages>' not in text
        if with_messages:
            assert payload['messages'][0]['attachment_ref'] == 'next'
            assert 'attachment_ref' not in payload['messages'][1]
            assert any('attachment_ref=next' in b.get('text', '') for b in blocks)
        assert ('后半句话' in text) == with_messages
        assert ('未收到新消息' in text) != with_messages
        assert any(b.get('tool_use_id') == 'stale' and b['is_error'] for b in blocks)
        assert (image in blocks) == with_messages
        assert daemon.incoming.empty()
        daemon.finish_active_turn()
        daemon.store.close()
    asyncio.run(run())


def test_wait_validation_cancellation_and_scheduler_continues(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        assert 'wait' in daemon.tool_surface.permitted_names('owner')
        assert 'wait' not in daemon.tool_surface.permitted_names('heartbeat')
        for seconds in (0, 61, True, 1.5):
            with pytest.raises(ValueError):
                await daemon.owner_updates.wait(seconds, [], daemon.channel.name)
        started = asyncio.Event()
        async def sleep(seconds):
            started.set()
            await asyncio.Event().wait()
        with patch('momoi.runtime.workflows.owner.updates.asyncio.sleep', sleep):
            task = asyncio.create_task(daemon.owner_updates.wait(60, [], daemon.channel.name))
            await started.wait()
            stop = asyncio.Event()
            checked = asyncio.Event()
            def close_idle_episodes():
                checked.set()
                stop.set()
                return []
            with patch.object(daemon.store, 'close_idle_episodes', side_effect=close_idle_episodes):
                scheduler = asyncio.create_task(daemon._scheduler_worker(stop))
                await asyncio.wait_for(checked.wait(), 2)
                assert not task.done()
                daemon.agenda_changed.set()
                await asyncio.wait_for(scheduler, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        daemon.store.close()
    asyncio.run(run())
