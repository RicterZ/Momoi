import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from momoi.models import IncomingMessage, ProviderResponse, ToolCall, TurnDraft
from momoi.observability.context import current_log_context
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent import TurnExecutionSpec
from tests.support import install_scripted_replyer, reply_call, with_owner_recall
from tests.test_episode_annealing import config


def response(calls):
    return ProviderResponse([{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.arguments}
                             for c in calls], calls)


@pytest.mark.parametrize('stage', ['goal', 'heartbeat', 'webhook'])
@pytest.mark.parametrize('stop_owner', [False, True])
def test_background_yields_and_rebuilds_after_owner(tmp_path, stage, stop_owner):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), owner_continuation_seconds=0))
    daemon.channel.quiet_seconds = daemon.channel.max_batch_seconds = 0
    install_scripted_replyer(daemon)
    calls = []
    owner_done = False
    started = False
    resumed = False
    begin_done = False
    draft = TurnDraft()
    end = ToolCall('end', 'end_turn', {} if stage == 'goal' else {'mood': {'decision': 'unchanged'}})

    async def read(call):
        calls.append(call.id)
        if call.id == 'done-read':
            await daemon._receive(IncomingMessage('owner-update', 'owner-update', '现在不用提醒了',
                                                  time.time(), 1, channel=daemon.channel.name))
        return {'ok': True, 'text': '已核实的事实'}

    async def complete(system, messages, tools, **kwargs):
        nonlocal started, owner_done, resumed, begin_done
        current = current_log_context()['stage']
        if current == 'owner':
            assert "[后台任务已暂停]" in json.dumps(messages, ensure_ascii=False)
            assert "已核实的事实" in json.dumps(messages, ensure_ascii=False)
            if stop_owner:
                await daemon._receive(IncomingMessage('stop', 'stop', '/stop', time.time(), 1,
                                                      channel=daemon.channel.name))
                await asyncio.sleep(0)
            owner_done = True
            return ProviderResponse([{'type': 'text', 'text': '知道了'}], [])
        if stage == 'heartbeat' and not begin_done:
            begin_done = True
            return response([ToolCall('begin', 'heartbeat_begin', {'activity': 'reading', 'mode': 'rest', 'strategy': []})])
        if not started:
            started = True
            return response([ToolCall('done-read', 'web_fetch', {'url': 'https://example.com'}),
                             *([reply_call('stale-reply', bubbles=['不要补发'])] if stage != 'heartbeat' else []),
                             ToolCall('not-run', 'web_fetch', {'url': 'https://example.com/next'})])
        assert owner_done
        text = json.dumps(messages, ensure_ascii=False)
        assert '现在不用提醒了' in text
        assert '已核实的事实' in text
        assert 'paused_for_owner' in text
        assert 'OLD_TRANSCRIPT_SENTINEL' not in text
        assert 'FRESH_TASK' in text
        receipt_index = next(i for i, message in enumerate(messages)
                             if '[后台任务执行记录]' in str(message.get('content'))
                             and '已核实的事实' in str(message.get('content')))
        owner_index = next(i for i, message in enumerate(messages)
                           if '现在不用提醒了' in str(message.get('content')))
        assert receipt_index < owner_index
        receipt = messages[receipt_index]['content']
        assert '记录时间：' in receipt
        payload = json.loads(receipt[receipt.index('{'):])
        assert payload['result']['ok'] is True
        assert payload['result']['text'] == '已核实的事实'
        assert '已核实的事实' not in messages[-1]['content']
        assert calls == ['done-read']
        resumed = True
        # These tests exercise scheduler/context behavior, not the stage's close schema.
        if stage == 'heartbeat' and draft.heartbeat_activity is None:
            return response([ToolCall('activity', 'heartbeat_activity', {
                'activity': 'reading', 'result': '无需提醒，已收尾', 'next_check_minutes': 30, 'reason': 'owner done'})])
        if stage == 'goal' and not draft.goals:
            return response([ToolCall('review', 'goal_review', {'status': 'done', 'result': 'owner handled'})])
        return response([end])

    async def run():
        daemon.provider = with_owner_recall(SimpleNamespace(complete=complete))
        daemon.store.begin_turn('background', stage, [])
        if stage == 'goal':
            daemon.agenda_tools.execute(ToolCall('create', 'goal_create', {
                'title': '提醒', 'success_criteria': '已提醒', 'next_action': '检查',
                'next_review_at': '2099-01-01T00:00:00+00:00'}), draft, source_event_id='setup')
            goal_id = next(iter(draft.goals))
            daemon.store.commit_goal_draft(draft)
            draft.goals.clear()
        else:
            goal_id = None
        work = daemon._run_tool_loop(daemon._system(planner=True),
                [{'role': 'user', 'content': 'OLD_TRANSCRIPT_SENTINEL'}],
                daemon.tool_surface.conversation_specs(), [], draft,
                execution=TurnExecutionSpec(stage, goal_id=goal_id), source_event_id='background',
                turn_id='background', delivery_channel=daemon.channel,
                resume_input=lambda: 'FRESH_TASK')
        daemon.start_active_turn(work, stage=stage, channel=daemon.channel.name)
        with patch.object(daemon.tool_batch.tool_executor.builtin_tools, 'execute', side_effect=read):
            if stop_owner:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(daemon.await_background_turn(asyncio.Event()), 5)
            else:
                await asyncio.wait_for(daemon.await_background_turn(asyncio.Event()), 5)
                assert resumed
            assert calls == ['done-read']
            assert daemon._background_pause is None
        daemon.finish_active_turn()
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()


def test_webhook_reply_generation_yields_without_sending_stale_text(tmp_path):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), owner_continuation_seconds=0))
    daemon.channel.quiet_seconds = daemon.channel.max_batch_seconds = 0
    install_scripted_replyer(daemon)
    from momoi.runtime.agent.delivery import BubbleDelivery
    daemon.bubble_delivery.wait_reply = BubbleDelivery.wait_reply.__get__(daemon.bubble_delivery)
    background_calls = 0
    owner_done = False

    async def generate(call, request):
        await daemon._receive(IncomingMessage('during-reply', 'during-reply', '不用通知我了',
                                              time.time(), 1, channel=daemon.channel.name))
        return ['旧通知']

    async def complete(system, messages, tools, **kwargs):
        nonlocal background_calls, owner_done
        if current_log_context()['stage'] == 'owner':
            text = json.dumps(messages, ensure_ascii=False)
            assert 'interrupted' in text
            assert '旧通知' not in text  # Only delivery receipts, never generated-but-unsent text.
            owner_done = True
            return ProviderResponse([{'type': 'text', 'text': '收到'}], [])
        background_calls += 1
        if background_calls == 1:
            return response([reply_call('notice', bubbles=['旧通知'])])
        assert owner_done
        assert '不用通知我了' in json.dumps(messages, ensure_ascii=False)
        assert '[后台任务恢复评估]' in json.dumps(messages, ensure_ascii=False)
        return ProviderResponse([{'type': 'text', 'text': '无需继续通知，结束事件处理'}], [])

    async def run():
        daemon.provider = with_owner_recall(SimpleNamespace(complete=complete))
        stop = asyncio.Event()
        future = asyncio.get_running_loop().create_future()
        daemon.webhook_requests.put_nowait(('检查并通知', 'webhook-paused', future))
        with patch.object(daemon.tool_batch.replyer, 'generate', side_effect=generate):
            worker = asyncio.create_task(daemon._agent_worker(stop))
            try:
                await asyncio.wait_for(asyncio.shield(future), 5)
                assert background_calls == 2
                assert not daemon.store.due_outbox()
                assert daemon.store.reply_delivery('webhook-paused', 'notice')['bubbles'] == []
            finally:
                stop.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()
