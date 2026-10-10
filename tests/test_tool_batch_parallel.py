import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from tests.support import install_scripted_replyer, reply_call, with_owner_recall
from tests.test_episode_annealing import config


@pytest.mark.parametrize("cancel", [False, True])
def test_reply_and_read_overlap_and_cancel_together(tmp_path, cancel):
    daemon = MomoiDaemon(config(str(tmp_path)))
    install_scripted_replyer(daemon)
    reply_started = asyncio.Event()
    read_started = asyncio.Event()
    finished = set()
    generate = daemon.tool_batch.replyer.generate
    rounds = 0

    async def reply(*args, **kwargs):
        reply_started.set()
        try:
            await read_started.wait()
            if cancel:
                await asyncio.Event().wait()
            return await generate(*args, **kwargs)
        finally:
            finished.add("reply")

    async def read(call):
        assert call.name == "web_fetch"
        read_started.set()
        try:
            await reply_started.wait()
            if cancel:
                await asyncio.Event().wait()
            return {"ok": True, "text": "搜索结果"}
        finally:
            finished.add("read")

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            calls = [reply_call("reply", bubbles=["我看看"]),
                     ToolCall("read", "web_fetch", {"url": "https://example.com"})]
        else:
            results = [block for message in messages
                       if isinstance(message.get("content"), list)
                       for block in message["content"] if block.get("type") == "tool_result"
                       and block.get("tool_use_id") in {"reply", "read"}]
            assert [block["tool_use_id"] for block in results] == ["reply", "read"]
            assert all(not block["is_error"] for block in results)
            assert "搜索结果" in json.dumps(results, ensure_ascii=False)
            assert finished == {"reply", "read"}
            calls = [ToolCall("end", "end_turn", {"mood": {"decision": "unchanged"}})]
        return ProviderResponse(
            [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
             for c in calls], calls)

    async def run():
        daemon.provider = with_owner_recall(SimpleNamespace(complete=complete))
        event = IncomingMessage("first", "first", "帮我看看", 1, 1, channel=daemon.channel.name)
        daemon.store.add_event(event)
        with patch.object(daemon.tool_batch.replyer, "generate", side_effect=reply), patch.object(
            daemon.tool_batch.tool_executor.builtin_tools, "execute", side_effect=read
        ):
            task = asyncio.create_task(daemon._complete_batch_turn([event], asyncio.Event(), "parallel"))
            try:
                await asyncio.wait_for(asyncio.gather(reply_started.wait(), read_started.wait()), 3)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert rounds == 1
                else:
                    await asyncio.wait_for(task, 3)
                    assert rounds == 2
                assert finished == {"reply", "read"}
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()


@pytest.mark.parametrize('arrival', ['model', 'tool'])
def test_owner_update_preserves_goal_and_skips_stale_reply(tmp_path, arrival):
    from datetime import datetime, timedelta, timezone
    daemon = MomoiDaemon(config(str(tmp_path)))
    install_scripted_replyer(daemon)
    first = IncomingMessage('first', 'first', '记得明天续写', 1, 1, channel=daemon.channel.name)
    update = IncomingMessage('update', 'update', '头发还湿着', 2, 2, channel=daemon.channel.name)
    rounds = 0

    async def read(call):
        await daemon._receive(update)
        return {'ok': True, 'text': '已查询'}

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            if arrival == 'model':
                await daemon._receive(update)
            calls = ([ToolCall('read', 'web_fetch', {'url': 'https://example.com'})]
                     if arrival == 'tool' else [])
            calls += [ToolCall('goal', 'goal_create', {
                'title': '续写', 'success_criteria': '完成下一篇', 'next_action': '续写下一篇',
                'next_review_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            }), reply_call('stale', bubbles=['旧回复'])]
        else:
            results = {b['tool_use_id']: json.loads(b['content']) for m in messages
                       if isinstance(m.get('content'), list) for b in m['content']
                       if b.get('type') == 'tool_result'}
            assert 'goal_id' in results['goal'], results['goal']
            assert results['stale']['error'] == 'superseded_by_owner_update'
            assert '未执行' in results['stale']['message']
            assert '[用户中途插话]' in json.dumps(messages, ensure_ascii=False)
            assert json.dumps(messages, ensure_ascii=False).count('头发还湿着') == 1
            calls = [ToolCall('end', 'end_turn', {'mood': {'decision': 'unchanged'}})]
        return ProviderResponse([{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.arguments}
                                 for c in calls], calls)

    async def run():
        daemon.provider = with_owner_recall(SimpleNamespace(complete=complete))
        daemon.store.add_event(first)
        with patch.object(daemon.tool_batch.tool_executor.builtin_tools, 'execute', side_effect=read):
            await asyncio.wait_for(daemon._complete_batch_turn([first], asyncio.Event(), 'steer-goal'), 5)
        assert rounds == 2
        assert daemon.store._db.execute("SELECT count(*) FROM goals WHERE title='续写'").fetchone()[0] == 1
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()
