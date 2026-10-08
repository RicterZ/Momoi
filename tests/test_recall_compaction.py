"""Recall evidence changes at compaction, never by a sliding age threshold."""
import copy
import json
from types import SimpleNamespace

from momoi.runtime.transcript.recall import compact_recall_messages
from momoi.storage import Store


def messages(turn_ids):
    rows = []
    for identifier in turn_ids:
        rows.extend([
            {'role': 'assistant', '_history_turn_ids': [identifier], 'content': [
                {'type': 'tool_use', 'id': 'recall-' + identifier, 'name': 'recall', 'input': {}}]},
            {'role': 'user', '_history_turn_ids': [identifier], 'content': [
                {'type': 'tool_result', 'tool_use_id': 'recall-' + identifier, 'content': json.dumps({
                    'ok': True, 'state': 'recalled', 'result_ref': 'tr_' + identifier,
                    'memory': [{'id': 1, 'key': 'example', 'content': '前' * 80 + '中' * 1000 + '后' * 80}],
                    'episodes': [{'id': 'episode', 'title': '示例话题', 'summary': '摘要' * 400,
                                  'turns': [{'messages': ['完整原文'], 'execution': ['工具过程']}]}],
                }, ensure_ascii=False)}]},
        ])
    return rows


def result(rows, turn_id):
    row = next(m for m in rows if m['role'] == 'user' and m['_history_turn_ids'] == [turn_id])
    return json.loads(row['content'][0]['content'])


def test_boundary_freezes_old_excerpts_and_preserves_recent_six_across_append_and_restart(tmp_path):
    path = tmp_path / 'db'
    store = Store(path)
    ids = [str(i) for i in range(10)]
    store.transcript_memory_context(ids)
    state = store.transcript_memory_context(ids, compact=True)
    assert state['recall_compacted_turn_ids'] == ids[:-6]
    before = messages(ids)
    compact_recall_messages(before, state['recall_compacted_turn_ids'])
    clipped = result(before, '0')
    assert clipped['history_truncated'] and clipped['result_ref'] == 'tr_0'
    assert clipped['episodes'][0]['id'] == 'episode'
    assert 'turns' not in clipped['episodes'][0]
    assert '[...truncated...]' in clipped['memory'][0]['content']
    assert 'history_truncated' not in result(before, '4')
    frozen = copy.deepcopy(before)
    store.close()
    store = Store(path)
    grown = ids + ['10', '11', '12']
    state = store.transcript_memory_context(grown)
    after = messages(grown)
    compact_recall_messages(after, state['recall_compacted_turn_ids'])
    assert after[:len(before)] == frozen
    state = store.transcript_memory_context(grown, compact=True)
    assert state['recall_compacted_turn_ids'] == grown[:-6]
    compact_recall_messages(after, state['recall_compacted_turn_ids'])
    assert result(after, '4')['history_truncated']
    assert 'history_truncated' not in result(after, '7')
    store.close()


def test_recall_failure_remains_exact_and_multiple_calls_in_turn_are_clipped():
    rows = messages(['old'])
    second = copy.deepcopy(rows)
    for row in second:
        for block in row['content']:
            if 'id' in block:
                block['id'] += '-2'
            if 'tool_use_id' in block:
                block['tool_use_id'] += '-2'
    failure = {'ok': False, 'error': 'network_failure', 'message': '错误细节' * 500}
    failed = copy.deepcopy(second)
    failed[1]['content'][0]['content'] = json.dumps(failure)
    rows += second + failed
    compact_recall_messages(rows, ['old'])
    assert json.loads(rows[1]['content'][0]['content'])['history_truncated']
    assert json.loads(rows[3]['content'][0]['content'])['history_truncated']
    assert json.loads(rows[5]['content'][0]['content']) == failure


def test_token_compaction_updates_same_frozen_recall_boundary(tmp_path):
    store = Store(tmp_path / 'db')
    ids = [str(i) for i in range(12)]
    state = store.transcript_memory_context(ids)
    store.fold_transcript_memory(state['revision'], retained_turn_ids=ids[2:])
    assert store.transcript_memory_context(ids[2:], track_boundary=False)['recall_compacted_turn_ids'] == ids[2:-6]
    store.close()
