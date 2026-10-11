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



def test_replyer_generates_with_malformed_legacy_event_payload(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        for channel in ('napcat', 'dashboard'):
            event = IncomingMessage(channel, channel, channel + ' input', 1, 1, channel=channel)
            store.add_event(event)
            store.begin_turn(channel, 'owner', [event.event_id])
            store.commit_turn([event], event.text, AgentReply([]), turn_id=channel)
        with store._db:
            store._db.execute("UPDATE events SET payload_json='not-json' WHERE id='napcat'")
        config = SimpleNamespace(soul_prompt_path=tmp_path / 'SOUL.md', soul_prompt='测试人格', timezone='UTC', thinking_stages={'replyer': 'low'})
        request = SimpleNamespace(turn_id='dashboard', round_number=1, delivery_channel=SimpleNamespace(name='dashboard'), current_events=[])
        async def complete(system, messages, tools):
            assert 'dashboard input' in str(messages)
            assert 'napcat input' not in str(messages)
            return ProviderResponse([{'type': 'text', 'text': '回复正常'}], [])
        replyer = Replyer(config, store, SimpleNamespace(complete=complete))
        assert asyncio.run(replyer.generate(ToolCall('reply-test', 'reply', {'intent': '回应', 'reference': ''}), request)) == ['回复正常']
    finally:
        store.close()

def test_replyer_isolated_request_and_actual_speech(tmp_path):
    (tmp_path / 'SOUL.md').write_text('测试人格')
    (tmp_path / 'REPLYER.md').write_text('测试表达')
    config = SimpleNamespace(soul_prompt_path=tmp_path / 'SOUL.md', soul_prompt='fallback',
                             timezone='UTC', thinking_stages={'replyer': 'low'})
    store = SimpleNamespace(replyer_history_rows=lambda channel: [], record_turn_usage=Mock(), emotion_context=lambda: "")
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


@pytest.mark.parametrize("delivery_context, expression", [({}, "实际回复"),
    ({"channel_notice": "message_recall"}, "实际回复"), ({"channel_notice": "poke"}, "qq://poke")])
@pytest.mark.parametrize("quote", [False, True])
def test_planner_replyer_dispatch_and_native_writeback(tmp_path, delivery_context, expression, quote):
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
    event = IncomingMessage('event', 'event', '测试输入', 1, 1, channel=daemon.channel.name,
                            delivery_context=delivery_context)
    daemon.store.add_event(event)
    daemon.store.begin_turn('owner-test', 'owner', ['event'])
    messages = [{'role': 'user', 'content': '测试输入'}]
    rounds = 0

    async def complete(system, history, tools, **kwargs):
        nonlocal rounds
        if not tools:
            assert current_log_context()['stage'] == 'replyer'
            return ProviderResponse([{'type': 'text', 'text': expression}], [], usage={'input': 8, 'output': 2})
        rounds += 1
        if rounds == 1:
            call = ToolCall('reply-one', 'reply', {'intent': '直接接住输入', 'reference': '',
                **({'reply_to_message_id': 'event'} if quote and not delivery_context else {})})
        else:
            assert expression in str(history)
            call = ToolCall('done', 'end_turn', {'mood': {'decision': 'unchanged'}})
        return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])

    daemon.provider = SimpleNamespace(complete=complete)
    daemon.tool_batch.replyer.provider = daemon.provider
    from tests.support import stub_reply_delivery_wait
    stub_reply_delivery_wait(daemon)
    try:
        asyncio.run(daemon._run_tool_loop(
            daemon._system(planner=True), messages, daemon.tool_surface.conversation_specs(),
            [event], TurnDraft(), execution=TurnExecutionSpec('owner', permitted_tools=daemon.tool_surface.permitted_names('owner')),
            source_event_id='event', turn_id='owner-test', delivery_channel=daemon.channel))
        assert rounds == 2
        assert expression in daemon.store._db.execute("SELECT text FROM turn_progress WHERE turn_id='owner-test'").fetchone()[0]
        if quote and not delivery_context:
            pending = daemon.store.due_outbox()[0]
            assert pending.payload['segments'][0] == {'type': 'reply', 'data': {'id': 'event'}}
        assert expression in str(messages)
        if expression == 'qq://poke':
            pending = daemon.store.due_outbox()[0]
            assert pending.kind == 'poke' and pending.payload == {'action': 'poke'}
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


@pytest.mark.parametrize("mode", ["text", "voice"])
def test_replyer_receives_mode_and_splits_bubbles(mode):
    config = SimpleNamespace(soul_prompt_path=None, soul_prompt="测试人格", timezone="Asia/Shanghai", thinking_stages={})
    store = SimpleNamespace(replyer_history_rows=lambda channel: [], record_turn_usage=Mock(), emotion_context=lambda: "")
    request = SimpleNamespace(delivery_channel=SimpleNamespace(name="test"), current_events=[],
                              turn_id="turn", round_number=1)

    async def complete(system, messages, tools):
        tail = messages[-1]["content"][0]["text"]
        assert f"发送形式：{'语音' if mode == 'voice' else '文字'}" in tail
        assert ("只输出适合朗读的实际发言" in tail) == (mode == "voice")
        assert ("只输出实际发言，用空行分隔气泡" in tail) == (mode == "text")
        assert tools == []
        return ProviderResponse([{"type": "text", "text": "第一句\n\n第二句"}], [])

    replyer = Replyer(config, store, SimpleNamespace(complete=complete))
    call = ToolCall("dispatch", "reply", {"intent": "回应", "reference": "", "mode": mode})
    actual = asyncio.run(replyer.generate(call, request))
    assert actual == ["第一句", "第二句"]


def test_reply_attachments_schema_preserves_media_but_rejects_voice_and_plain_strings():
    from momoi.runtime.tool_contracts.reply import REPLY_TOOL_SPEC
    from momoi.tools.validation import validate_tool_arguments
    schema = REPLY_TOOL_SPEC["input_schema"]
    media = {"segments": [{"type": "image", "data": {"file": "/tmp/image.png"}}]}
    args = {"intent": "发图", "reference": "", "attachments": [media]}
    normalized, error = validate_tool_arguments("reply", args, schema)
    assert error is None and normalized == args
    for invalid in ({**args, "mode": "voice"}, {**args, "attachments": ["完整发言绕过生成"]}, {**args, "attachments": ["emotion://normal"]},
                    {**args, "attachments": [{"segments": [{"type": "text", "data": {"text": "绕过生成"}}]}]}):
        assert validate_tool_arguments("reply", invalid, schema)[1] is not None


def test_replyer_voice_output_reaches_tts_and_native_transcript(tmp_path):
    from unittest.mock import AsyncMock
    from momoi.config.models import AppConfig
    from momoi.integrations.models import LLMConfig
    from momoi.channel.napcat import NapCatConfig
    from momoi.integrations.contracts.tts import AudioOutput
    from momoi.runtime import MomoiDaemon
    from tests.support import provider_catalog

    tts = SimpleNamespace(synthesize=AsyncMock(return_value=AudioOutput(b"audio", "silk")))
    daemon = MomoiDaemon(AppConfig(
        providers=provider_catalog(LLMConfig('http://localhost', 'test', 'model', 100, 0, 1, 0)),
        channel=NapCatConfig('ws://localhost', 'test', 1, 60, 30, 30, 20),
        transcript_turns_min=8, transcript_turns_max=32, episode_unsummarized_tail_turns=2, memory_results=2,
        soul_prompt='测试人格', system_prompt='测试规则', database=tmp_path / 'db', log_level='INFO'),
        tts_provider=tts)
    event = IncomingMessage('voice-event', 'voice-event', '语音回应', 1, 1, channel=daemon.channel.name)
    daemon.store.add_event(event)
    daemon.store.begin_turn('voice-owner', 'owner', ['voice-event'])
    messages = [{'role': 'user', 'content': '语音回应'}]
    rounds = 0

    async def complete(system, history, tools, **kwargs):
        nonlocal rounds
        if not tools:
            assert '发送形式：语音' in str(history[-1])
            return ProviderResponse([{'type': 'text', 'text': '生成的语音第一句。\n\n生成的第二句。'}], [])
        assert not {'send_bubbles', 'send_voice'} & {tool['name'] for tool in tools}
        rounds += 1
        if rounds == 1:
            call = ToolCall('voice-dispatch', 'reply', {'intent': '接住语音请求', 'reference': '', 'mode': 'voice'})
        else:
            assert '生成的第二句' in str(history)
            call = ToolCall('done', 'end_turn', {'mood': {'decision': 'unchanged'}})
        return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])

    daemon.provider = SimpleNamespace(complete=complete)
    daemon.tool_batch.replyer.provider = daemon.provider
    from tests.support import stub_reply_delivery_wait
    stub_reply_delivery_wait(daemon)
    try:
        asyncio.run(daemon._run_tool_loop(
            daemon._system(planner=True), messages, daemon.tool_surface.conversation_specs(),
            [event], TurnDraft(), execution=TurnExecutionSpec('owner', permitted_tools=daemon.tool_surface.permitted_names('owner')),
            source_event_id='voice-event', turn_id='voice-owner', delivery_channel=daemon.channel))
        speech = '生成的语音第一句。\n\n生成的第二句。'
        tts.synthesize.assert_awaited_once_with(speech)
        assert [(row.kind, row.text) for row in daemon.store.due_outbox()] == [('voice', speech)]
        exchanges = daemon.store.turn_exchanges(['voice-owner'])['voice-owner']
        assert any('生成的语音第一句' in str(exchange['results']) for exchange in exchanges)
        assert rounds == 2
    finally:
        daemon.store.close()


@pytest.mark.parametrize("mode", ["text", "voice"])
def test_emotion_catalog_stays_in_shared_replyer_prefix(mode):
    config = SimpleNamespace(soul_prompt_path=None, soul_prompt="测试人格", timezone="Asia/Shanghai", thinking_stages={})
    store = SimpleNamespace(replyer_history_rows=lambda channel: [], record_turn_usage=Mock(),
                            emotion_context=lambda: "emotion://happy 开心时使用")
    request = SimpleNamespace(delivery_channel=SimpleNamespace(name="test"), current_events=[], turn_id="t", round_number=1)
    async def complete(system, messages, tools):
        assert "<emotion_catalog>" in system
        return ProviderResponse([{"type": "text", "text": "开心！\n\nemotion://happy\n\n下一句" if mode == "text" else "开心！"}], [])
    actual = asyncio.run(Replyer(config, store, SimpleNamespace(complete=complete)).generate(
        ToolCall("c", "reply", {"intent": "回应", "reference": "", "mode": mode}), request))
    assert actual == (["开心！", "emotion://happy", "下一句"] if mode == "text" else ["开心！"])


def test_replyer_text_and_voice_have_identical_system_and_history():
    config = SimpleNamespace(soul_prompt_path=None, soul_prompt="测试人格", timezone="Asia/Shanghai", thinking_stages={})
    store = SimpleNamespace(replyer_history_rows=lambda channel: [], record_turn_usage=Mock(),
                            emotion_context=lambda: "emotion://happy 开心时使用")
    request = SimpleNamespace(delivery_channel=SimpleNamespace(name="test"), current_events=[], turn_id="t", round_number=1)
    requests = []
    async def complete(system, messages, tools):
        requests.append((system, messages, tools))
        return ProviderResponse([{"type": "text", "text": "实际发言"}], [])
    replyer = Replyer(config, store, SimpleNamespace(complete=complete))
    for mode in ("text", "voice"):
        asyncio.run(replyer.generate(ToolCall("c", "reply", {"intent": "回应", "reference": "", "mode": mode}), request))
    assert requests[0][0] == requests[1][0]
    assert requests[0][1][:-1] == requests[1][1][:-1]
    assert requests[0][2] == requests[1][2] == []
    assert requests[0][1][-1] != requests[1][1][-1]


@pytest.mark.parametrize("channel_name, mode, output, accepted", [
    ("napcat", "text", "qq://poke", True),
    ("napcat", "voice", "qq://poke", False),
    ("other", "text", "qq://poke", False),
    ("napcat", "text", "hello qq://poke", False),
])
def test_replyer_poke_is_an_action_not_spoken_or_literal_text(channel_name, mode, output, accepted):
    config = SimpleNamespace(soul_prompt_path=None, soul_prompt='test', timezone='UTC', thinking_stages={})
    store = SimpleNamespace(replyer_history_rows=lambda channel: [], record_turn_usage=Mock(),
                            emotion_context=lambda: '')
    channel = SimpleNamespace(name=channel_name, content_blocks=lambda _: [])
    if channel_name == 'napcat':
        channel.poke_owner = Mock()
    request = SimpleNamespace(turn_id='turn', round_number=1, delivery_channel=channel, current_events=[])
    async def complete(system, messages, tools):
        assert tools == []
        assert ('<qq_expression>' in system) == (channel_name == 'napcat')
        return ProviderResponse([{'type': 'text', 'text': output}], [])
    replyer = Replyer(config, store, SimpleNamespace(complete=complete))
    call = ToolCall('reply', 'reply', {'intent': 'test', 'reference': '', 'mode': mode})
    if accepted:
        assert asyncio.run(replyer.generate(call, request)) == ['qq://poke']
    else:
        with pytest.raises(ValueError):
            asyncio.run(replyer.generate(call, request))


def test_qq_replyer_poke_capability_prefix_is_identical_for_text_and_phone():
    config = SimpleNamespace(soul_prompt_path=None, soul_prompt='test', timezone='UTC', thinking_stages={})
    store = SimpleNamespace(replyer_history_rows=lambda _: [], record_turn_usage=Mock(), emotion_context=lambda: '')
    prefixes = []
    async def complete(system, messages, tools):
        prefixes.append(system)
        return ProviderResponse([{'type': 'text', 'text': 'test'}], [])
    replyer = Replyer(config, store, SimpleNamespace(complete=complete))
    for channel, mode in [(SimpleNamespace(name='napcat', poke_owner=Mock()), 'text'),
                          (SimpleNamespace(name='qq_call', dialogue_channel='napcat'), 'voice')]:
        request = SimpleNamespace(turn_id='turn', round_number=1, delivery_channel=channel, current_events=[])
        asyncio.run(replyer.generate(ToolCall('reply', 'reply', {'intent': 'test', 'reference': '', 'mode': mode}), request))
    assert prefixes[0] == prefixes[1]
    assert '<qq_expression>' in prefixes[0]


def test_optional_qq_quote_targets_selected_message_and_keeps_other_bubbles_plain():
    from momoi.channel.napcat import NapCatChannel, NapCatConfig
    from momoi.runtime.tool_contracts.reply import REPLY_TOOL_SPEC
    from momoi.tools.validation import validate_tool_arguments
    config = SimpleNamespace(soul_prompt_path=None, soul_prompt='测试', timezone='UTC', thinking_stages={})
    store = SimpleNamespace(replyer_history_rows=lambda channel: [], record_turn_usage=Mock(), emotion_context=lambda: '')
    channel = NapCatChannel(NapCatConfig('ws://localhost', '20000', 1, 60, 30, 30, 20))
    events = [IncomingMessage('one', '101', '第一条', 1, 1, channel='napcat'),
              IncomingMessage('two', '102', '第二条', 2, 2, channel='napcat')]
    request = SimpleNamespace(delivery_channel=channel, current_events=events, turn_id='quote', round_number=1)
    async def complete(*args):
        return ProviderResponse([{'type': 'text', 'text': '重点回应\n\n后续补充'}], [])
    replyer = Replyer(config, store, SimpleNamespace(complete=complete))
    args = {'intent': '突出第一条', 'reference': '', 'reply_to_message_id': '101'}
    assert validate_tool_arguments('reply', args, REPLY_TOOL_SPEC['input_schema'])[1] is None
    assert validate_tool_arguments('reply', {**args, 'mode': 'voice'}, REPLY_TOOL_SPEC['input_schema'])[1] is not None
    result = asyncio.run(replyer.generate(ToolCall('quote-call', 'reply', args), request))
    assert result == [{'segments': [{'type': 'reply', 'data': {'id': '101'}},
                                   {'type': 'text', 'data': {'text': '重点回应'}}]}, '后续补充']
    with pytest.raises(ValueError, match='quote target'):
        asyncio.run(replyer.generate(ToolCall('bad', 'reply', {**args, 'reply_to_message_id': '999'}), request))
    channel.is_quote_target = lambda message_id: message_id == '777'
    old = asyncio.run(replyer.generate(ToolCall('historical', 'reply', {**args, 'reply_to_message_id': '777'}), request))
    assert old[0]['segments'][0] == {'type': 'reply', 'data': {'id': '777'}}
    channel.is_message_recalled = lambda channel, message_id: message_id == '101'
    with pytest.raises(ValueError, match='quote target'):
        asyncio.run(replyer.generate(ToolCall('recalled', 'reply', args), request))
