from copy import deepcopy

"""Historical chat projection must preserve delivery evidence and tool protocol."""
import copy
import json
from zoneinfo import ZoneInfo

import pytest

from momoi.models import AgentReply, IncomingMessage
from momoi.runtime.transcript.native import render_exchanges
from momoi.storage import Store

TZ = ZoneInfo('Asia/Shanghai')


def exchange(name='reply', *, identifier='r', ok=True, speech=None):
    item = {
        'content': [{'type': 'text', 'text': '回复策划，不是实际发言'},
                    {'type': 'tool_use', 'id': identifier, 'name': name,
                     'input': {'intent': '策划内容', 'reference': '旧依据'}}],
        'results': [{'type': 'tool_result', 'tool_use_id': identifier,
                     'content': json.dumps({'ok': ok, 'state': 'committed',
                                            'bubbles': ['生成结果不是投递证据']})}],
    }
    if speech is not None:
        item['_reply_messages'] = {identifier: speech}
    return item


def speech(text='已完成校验', state='delivered', kind='text', at=1):
    return {'content': text, 'delivery_state': state, 'kind': kind, 'created_at': at}


def calls(messages):
    return [block for message in messages if message['role'] == 'assistant'
            for block in message['content'] if block.get('type') == 'tool_use']


def results(messages):
    return [block for message in messages if message['role'] == 'user'
            for block in message['content'] if block.get('type') == 'tool_result']


def test_reply_receipt_preserves_full_generated_message_and_planner_without_mutation():
    text = '完整生成消息' * 1000
    item = exchange(speech=[speech('数据库里的另一种渲染')])
    item['results'][0]['content'] = json.dumps({'ok': True, 'state': 'committed', 'mode': 'voice',
        'bubbles': [text, 'emotion://happy'], 'result_ref': 'tr_reply'})
    original = [item, exchange('end_turn', identifier='e')]
    frozen = copy.deepcopy(original)
    messages = render_exchanges(original, has_speech=True)
    assert original == frozen
    assert len(calls(messages)) == len(results(messages)) == 1
    assert calls(messages)[0]['input'] == {'intent': '策划内容', 'reference': '旧依据'}
    result = json.loads(results(messages)[0]['content'])
    assert result['bubbles'] == [text, 'emotion://happy']
    assert result['mode'] == 'voice' and result['result_ref'] == 'tr_reply'
    assert 'history_truncated' not in result and 'preview' not in result
    assert messages[0]['content'][0]['text'] == '回复策划，不是实际发言'
    assert '<sent_reply>' not in str(messages)
    assert text not in str(messages[0])


@pytest.mark.parametrize('mode', ['failure', 'missing_archive', 'missing_result', 'delivery_failed'])
def test_unconfirmed_replies_keep_original_call_and_observation(mode):
    item = exchange(ok=mode != 'failure', speech=[speech(state='failed' if mode == 'delivery_failed' else 'delivered')])
    if mode == 'missing_archive':
        item.pop('_reply_messages')
    if mode == 'missing_result':
        item['results'] = []
    messages = render_exchanges([item], history_format=4)
    assert calls(messages)[0]['input']['intent'] == '策划内容'
    assert '<sent_reply>' not in str(messages)
    if mode == 'missing_result':
        assert json.loads(results(messages)[0]['content'])['error'] == 'execution_result_unknown'
    if mode == 'delivery_failed':
        assert json.loads(results(messages)[0]['content'])['delivery_state'] == 'failed'


def test_mixed_business_batch_keeps_native_order_and_both_call_pairs():
    item = exchange(speech=[speech()])
    item['content'].append({'type': 'tool_use', 'id': 'read', 'name': 'read_file', 'input': {'path': '/example.txt'}})
    item['results'].append({'type': 'tool_result', 'tool_use_id': 'read', 'content': '{"ok":true,"content":"evidence"}'})
    messages = render_exchanges([item], history_format=4)
    assert [c['id'] for c in calls(messages)] == ['r', 'read']
    assert [r['tool_use_id'] for r in results(messages)] == ['r', 'read']
    assert calls(messages)[0]['input']['reference'] == '旧依据'
    assert '<sent_reply>' not in str(messages)


def test_end_turn_failure_preserved_and_successful_silence_explicit():
    failed = render_exchanges([exchange('end_turn', ok=False)], history_format=4)
    assert calls(failed)[0]['name'] == 'end_turn'
    successful = render_exchanges([exchange('end_turn')], history_format=4)
    assert not calls(successful) and not results(successful)
    assert 'ended the Turn without replying' in str(successful)


def test_existing_window_removes_end_turn_and_keeps_full_reply_receipt():
    item = exchange(speech=[speech()])
    messages = render_exchanges([item, exchange('end_turn', identifier='e')], history_format=3, has_speech=True)
    assert [c['name'] for c in calls(messages)] == ['reply']
    assert calls(messages)[0]['input']['intent'] == '策划内容'
    assert json.loads(results(messages)[0]['content'])['bubbles'] == ['生成结果不是投递证据']
    assert '<sent_reply>' not in str(messages)


def test_reply_archive_ids_are_local_to_executor_and_include_followups(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        event = IncomingMessage('event', 'owner', '开始示例任务', 1, 1)
        store.add_event(event)
        store.commit_turn([event], event.text, AgentReply([]), turn_id='owner')
        for turn, text in [('owner', '已开始'), ('followup', '已完成')]:
            if turn == 'followup':
                store.begin_turn(turn, 'reply_followup', ['owner'])
            store.queue_progress(turn, 'same-id', [text], 'napcat')
            outbox = store._db.execute('SELECT id FROM outbox WHERE turn_id=?', (turn,)).fetchone()[0]
            with store._db:
                store._db.execute("INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json,outbox_id,delivery_state) VALUES ('owner','assistant',?,2,'[]',?,'delivered')", (text, outbox))
                store._db.execute("UPDATE turns SET state='completed' WHERE id=?", (turn,))
            store.append_turn_journal(turn, 'assistant_exchange', exchange(identifier='same-id'), trust='runtime')
        old = store.turn_exchanges(['owner'])['owner']
        enriched = store.turn_exchanges(['owner'], include_reply_messages=True)['owner']
        assert '_reply_messages' not in old[0]
        assert [x['_reply_messages']['same-id'][0]['content'] for x in enriched] == ['已开始', '已完成']
        rendered = render_exchanges(enriched, history_format=4, has_speech=True)
        assert len(calls(rendered)) == 2
        assert all(c['input']['intent'] == '策划内容' for c in calls(rendered))
        assert store.turn_exchanges(['owner'])['owner'] == old
    finally:
        store.close()


def test_large_failed_voice_keeps_delivery_failure_after_result_clipping():
    item = exchange(speech=[speech('长语音', 'failed', 'voice')])
    item['results'][0]['content'] = json.dumps({'ok': True, 'state': 'committed', 'bubbles': ['正文' * 1000]})
    messages = render_exchanges([item])
    assert json.loads(results(messages)[0]['content'])['delivery_state'] == 'failed'
    assert '<sent_reply>' not in str(messages)


def test_context_pressure_does_not_clip_reply_receipt():
    from types import SimpleNamespace
    from momoi.runtime.agent.context_window import ContextWindow
    text = '完整回复内容' * 2000
    item = exchange()
    item['results'][0]['content'] = json.dumps({'ok': True, 'bubbles': [text]})
    messages = render_exchanges([item], mark_silence=False)
    window = ContextWindow(SimpleNamespace(max_input_tokens=800, context_compaction_ratio=1), None, None)
    window.fit([], messages, [], 0)
    assert json.loads(results(messages)[0]['content'])['bubbles'] == [text]


def test_replay_pairs_results_and_preserves_orphan_evidence_as_text():
    item = {'content': [
        {'type': 'tool_use', 'id': name, 'name': 'exec', 'input': {}}
        for name in ('a', 'b', 'a')], 'results': [
        {'type': 'tool_result', 'tool_use_id': name, 'content': json.dumps({'ok': True, 'value': name})}
        for name in ('b', 'orphan', 'a', 'a')]}
    original = deepcopy(item)
    messages = render_exchanges([item])
    native_calls = calls(messages)
    native_results = results(messages)
    assert [c['id'] for c in native_calls] == ['a', 'b']
    assert [r['tool_use_id'] for r in native_results] == ['a', 'b']
    assert 'unpaired historical tool result' in str(messages)
    assert 'orphan' in str(messages)
    assert item == original
