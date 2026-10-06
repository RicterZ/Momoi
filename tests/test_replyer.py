import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall, TurnDraft
from momoi.runtime.agent.replyer import Replyer
from momoi.runtime.agent import TurnExecutionSpec
from momoi.runtime.workflows.memory_operation.conversation import conversation_message
from momoi.storage import Store
from momoi.integrations.request_context import requested_thinking_effort
from momoi.observability.context import current_log_context


def test_replyer_channel_history_uses_actual_delivery(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        for channel in ('first', 'second'):
            event = IncomingMessage(channel, channel, channel + ' input', 1, 1, channel=channel)
            store.add_event(event)
            store.begin_turn(channel, "owner", [event.event_id])
            store.queue_progress(channel, channel + '-send', [channel + ' output'], channel)
            store.commit_turn([event], event.text, AgentReply([]), turn_id=channel)
        with store._db:
            store._db.execute("UPDATE messages SET delivery_state='delivered' WHERE role='assistant' AND turn_id='first'")
        rows = store.replyer_dialogue_rows('first')
        assert [r['content'] for r in rows] == ['first input', 'first output']
        assert [r['content'] for r in store.replyer_dialogue_rows('second')] == ['second input']
        assert store.committed_reply('first', 'first-send')['bubbles'] == ['first output']
        assert store.committed_reply('first', 'missing') is None
        first = store.replyer_history_rows('first')
        assert first == rows
        assert [r['content'] for r in store.replyer_history_rows('second')] == ['second input']
        store.close()
        store = Store(tmp_path / 'db')
        assert store.replyer_history_rows('first') == first
    finally:
        store.close()


def test_replyer_isolated_request_and_actual_speech(tmp_path):
    (tmp_path / 'SOUL.md').write_text('测试人格')
    (tmp_path / 'REPLYER.md').write_text('测试表达')
    config = SimpleNamespace(soul_prompt_path=tmp_path / 'SOUL.md', soul_prompt='fallback',
                             timezone='UTC', thinking_stages={'replyer': 'low'})
    store = SimpleNamespace(replyer_history_rows=lambda channel: [], record_turn_usage=Mock())
    channel = SimpleNamespace(name='test', content_blocks=lambda segments: [{'type': 'image', 'source': {'type': 'url', 'url': 'https://example.invalid/image'}}])
    request = SimpleNamespace(turn_id='turn', round_number=2, delivery_channel=channel,
                             current_events=[IncomingMessage('e', 'e', '测试图片', 1, 1, ({'type': 'image'},))])

    async def complete(system, messages, tools):
        assert tools == []
        assert system == '测试人格\n\n测试表达'
        assert requested_thinking_effort() == 'low'
        assert current_log_context()['stage'] == 'replyer'
        assert current_log_context()['tool_call_id'] == 'c'
        assert any(b['type'] == 'image' for b in messages[-1]['content'])
        assert 'memory' not in str(messages)
        return ProviderResponse([{'type': 'text', 'text': '自己的反应\n\n另一句'}], [], usage={'input': 10, 'output': 4})

    replyer = Replyer(config, store, SimpleNamespace(complete=complete))
    bubbles = asyncio.run(replyer.generate(ToolCall('c', 'reply', {'intent': '接住情绪', 'reference': ''}), request))
    assert bubbles == ['自己的反应', '另一句']
    store.record_turn_usage.assert_called_once_with('turn', 10, 4)
    assert requested_thinking_effort('default') == 'default'
    (tmp_path / 'SOUL.md').write_text('更新人格')
    async def empty(*args):
        assert args[0].startswith('更新人格')
        return ProviderResponse([], [])
    replyer.provider.complete = empty
    with pytest.raises(ValueError, match='empty'):
        asyncio.run(replyer.generate(ToolCall('c', 'reply', {'intent': '回应', 'reference': ''}), request))


def test_memory_evidence_reads_reply_result_not_intent():
    messages = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'reply', 'name': 'reply',
                   'input': {'intent': '内部派发方向', 'reference': '内部参考'}}]},
                {'role': 'tool', 'tool_call_id': 'reply', 'content': '{"ok":true,"bubbles":["实际发言"]}'}]
    evidence = conversation_message(messages)['content']
    assert '实际发言' in evidence
    assert '内部派发方向' not in evidence


def test_planner_replyer_dispatch_and_native_writeback(tmp_path):
    from momoi.config.models import AppConfig
    from momoi.integrations.models import LLMConfig
    from momoi.channel.napcat import NapCatConfig
    from momoi.runtime import MomoiDaemon
    from tests.support import provider_catalog

    daemon = MomoiDaemon(AppConfig(
        providers=provider_catalog(LLMConfig('http://localhost', 'test', 'model', 100, 0, 1, 0)),
        channel=NapCatConfig('ws://localhost', 'test', 1, 60, 30, 30, 20),
        transcript_turns_min=8, transcript_turns_max=32, episode_unsummarized_tail_turns=2, memory_results=2,
        soul_prompt='测试人格', system_prompt='测试规则', database=tmp_path / 'db', log_level='INFO'))
    event = IncomingMessage('event', 'event', '测试输入', 1, 1, channel=daemon.channel.name)
    daemon.store.add_event(event)
    daemon.store.begin_turn('owner-test', 'owner', ['event'])
    messages = [{'role': 'user', 'content': '测试输入'}]
    rounds = 0

    async def complete(system, history, tools, **kwargs):
        nonlocal rounds
        if not tools:
            assert current_log_context()['stage'] == 'replyer'
            return ProviderResponse([{'type': 'text', 'text': '实际回复'}], [], usage={'input': 8, 'output': 2})
        rounds += 1
        if rounds == 1:
            call = ToolCall('reply-one', 'reply', {'intent': '直接接住输入', 'reference': ''})
        else:
            assert '实际回复' in str(history)
            call = ToolCall('done', 'end_turn', {'reply_wait': {'wait': False}, 'mood': {'decision': 'unchanged'}})
        return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])

    daemon.provider = SimpleNamespace(complete=complete)
    daemon.tool_batch.replyer.provider = daemon.provider
    try:
        asyncio.run(daemon._run_tool_loop(
            daemon._system(planner=True), messages, daemon.tool_surface.conversation_specs(),
            [event], TurnDraft(), execution=TurnExecutionSpec('owner', permitted_tools=daemon.tool_surface.permitted_names('owner')),
            source_event_id='event', turn_id='owner-test', delivery_channel=daemon.channel))
        assert rounds == 2
        assert daemon.store._db.execute("SELECT text FROM turn_progress WHERE turn_id='owner-test'").fetchone()[0] == '实际回复'
        assert '实际回复' in str(messages)
        assert daemon.store.turn_usage('owner-test')['llm_calls'] == 3
    finally:
        daemon.store.close()


def test_thinking_trace_survives_reopen_and_old_records(tmp_path):
    import time
    path = tmp_path / 'db'
    store = Store(path)
    now = time.time()
    trace = {'parent_call_id': 'planner', 'tool_call_id': 'dispatch', 'dump_file': 'request.json',
             'tool_calls': [{'id': 'action', 'name': 'reply', 'arguments': {'intent': '测试意图', 'reference': ''}}]}
    store.record_thinking_call(created_at=now, turn_id='t', call_id='new', stage='replyer', trace=trace)
    store.record_thinking_call(created_at=now, turn_id='t', call_id='old', stage='owner')
    store.close()
    reopened = Store(path)
    try:
        calls = {call['call_id']: call for call in reopened.read_thinking('t')['calls']}
        assert calls['new']['trace'] == trace
        assert calls['old']['trace'] == {}
    finally:
        reopened.close()


def test_replyer_window_migrates_existing_database(tmp_path):
    from momoi.storage.core.migrations import SCHEMA_VERSION
    path = tmp_path / 'existing.db'
    store = Store(path)
    with store._db:
        store._db.execute('DROP TABLE replyer_history_windows')
        store._db.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
    store.close()
    store = Store(path)
    try:
        assert store.replyer_history_rows('test') == []
        assert store._db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    finally:
        store.close()
