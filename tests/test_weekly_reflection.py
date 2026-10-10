import asyncio
from dataclasses import replace
from unittest.mock import patch

from momoi.config.models import ReflectionConfig
from momoi.models import ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.workflows.weekly_reflection import parse_weekly_reflection, weekly_reflection_input
from momoi.storage import Store
from tests.test_episode_annealing import config
from tests.test_weekly_reflection_storage import seed, stamp


def test_weekly_validation_and_daily_rendering(tmp_path):
    store = Store(tmp_path / 'db', timezone='Asia/Shanghai')
    seed(store, '2026-10-06', '<日一>')
    source = store.weekly_reflection_source('2026-10-11')
    content = weekly_reflection_input(source)
    assert '&lt;日一&gt;' in content and 'date="2026-10-06"' in content
    finding = dict(triggers=['回复'], key='topic', kind='preference', content='用户明确要求简短回复。', events=[{'refs': ['observation:1'], 'summary': '要求简短'}], conflicts=[])
    args = {'summary': '仅有一天材料', 'findings': [finding]}
    assert parse_weekly_reflection(args)[1] is None
    for bad in [dict(triggers=['回复', '聊天', '表达']), dict(triggers=['回复', '回复']), dict(triggers=[' ']), dict(content=' '), dict(key=' '), dict(assessment='invented'), dict(evidence_ids=['invented'])]:
        assert parse_weekly_reflection({'summary': '回顾', 'findings': [{**finding, **bad}]})[1]
    assert parse_weekly_reflection({'summary': '回顾', 'findings': [finding, finding]})[1]
    assert parse_weekly_reflection({'summary': ' ', 'findings': []})[1]
    store.close()


def test_weekly_workflow_commits_and_retries_failure(tmp_path):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), timezone='Asia/Shanghai', reflection=ReflectionConfig(enabled=True)))
    seed(daemon.store, '2026-10-08')
    now = stamp('2026-10-11T05:00:00')
    daemon.store.claim_due_weekly_reflection(daemon.config.reflection, now)

    async def fail(*args, **kwargs):
        raise RuntimeError('provider failed')

    daemon._run_agent_workflow = fail
    with patch('momoi.storage.reflection.weekly.time.time', return_value=now):
        asyncio.run(daemon._complete_weekly_reflection_turn('2026-10-11', asyncio.Event()))
    assert daemon.store.weekly_reflection('2026-10-11')['state'] == 'pending'
    assert daemon.store.claim_due_weekly_reflection(daemon.config.reflection, now + 1) is None
    retry_at = daemon.store.weekly_reflection('2026-10-11')['retry_at']
    daemon.store.claim_due_weekly_reflection(daemon.config.reflection, max(now + 1, retry_at))

    async def finish(system, messages, tools, *, turn_id, workflow):
        assert 'date="2026-10-08"' in messages[0]['content']
        assert [t['name'] for t in tools] == ['weekly_reflection_finish']
        assert (await workflow.execute_tool(ToolCall('finish', 'weekly_reflection_finish', {
            'summary': '只有一天材料，继续观察', 'findings': [],
        })))['ok']
        return workflow.completion_result()

    daemon._run_agent_workflow = finish
    asyncio.run(daemon._complete_weekly_reflection_turn('2026-10-11', asyncio.Event()))
    assert daemon.store.weekly_reflection('2026-10-11')['state'] == 'completed'
    assert not daemon.store.list_goals()
    daemon.store.close()


def test_scheduler_enqueues_weekly_review(tmp_path):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), reflection=ReflectionConfig(enabled=True)))

    async def run():
        stop = asyncio.Event()
        def claim(_config):
            stop.set()
            return {'period_end': '2026-10-11'}
        with patch.object(daemon.store, 'claim_due_reflection', return_value=None), \
             patch.object(daemon.store, 'claim_due_weekly_reflection', side_effect=claim):
            await daemon._scheduler_worker(stop)
        job = daemon.autonomous.get_nowait()
        assert job.kind == 'weekly_reflection' and job.id == '2026-10-11'
        daemon._release_autonomous_claim(job)
    asyncio.run(run())
    daemon.store.close()


def test_weekly_runs_through_real_agent_loop_and_handles_cancellation(tmp_path):
    from momoi.models import ProviderResponse
    daemon = MomoiDaemon(replace(config(str(tmp_path)), timezone='Asia/Shanghai', reflection=ReflectionConfig(enabled=True)))
    seed(daemon.store, '2026-10-08')
    daemon.store.claim_due_weekly_reflection(daemon.config.reflection, stamp('2026-10-11T05:00:00'))

    class Provider:
        async def complete(self, system, messages, tools, **kwargs):
            from momoi.integrations.request_context import requested_thinking_effort
            assert requested_thinking_effort() == 'low'
            call = ToolCall('weekly-done', 'weekly_reflection_finish', {'summary': '盘点完成', 'findings': []})
            return ProviderResponse(content=[{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], tool_calls=[call])
    daemon.provider = Provider()
    asyncio.run(daemon._complete_weekly_reflection_turn('2026-10-11', asyncio.Event()))
    assert daemon.store.weekly_reflection('2026-10-11')['state'] == 'completed'
    seed(daemon.store, '2026-10-12')
    daemon.store.claim_due_weekly_reflection(daemon.config.reflection, stamp('2026-10-18T05:00:00'))

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    daemon._run_agent_workflow = cancel
    import pytest
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._complete_weekly_reflection_turn('2026-10-18', asyncio.Event()))
    assert daemon.store.weekly_reflection('2026-10-18')['state'] == 'pending'
    daemon.store.close()
