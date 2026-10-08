"""Previous-turn recalls are excerpts; current-turn evidence stays complete."""
import copy
import json

from momoi.runtime.transcript.recall import compact_recall_messages


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


def test_previous_turns_are_clipped_but_current_turn_stays_complete():
    history = messages(['old', 'recent'])
    current = messages(['current'])
    rows = history + current
    compact_recall_messages(rows, ['old', 'recent'])
    for identifier in ['old', 'recent']:
        clipped = result(rows, identifier)
        assert clipped['history_truncated']
        assert clipped['result_ref'] == 'tr_' + identifier
        assert 'turns' not in clipped['episodes'][0]
        assert '[...truncated...]' in clipped['memory'][0]['content']
    assert 'history_truncated' not in result(rows, 'current')
    frozen = copy.deepcopy(rows[:len(history)])
    compact_recall_messages(rows, ['old', 'recent', 'current'])
    assert rows[:len(history)] == frozen
    assert result(rows, 'current')['history_truncated']


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


def test_paged_recall_resolves_original_or_keeps_small_preview(tmp_path):
    original = result(messages(['old']), 'old')
    from unittest.mock import Mock
    snapshots = Mock()
    snapshots.historical_payload.return_value = original
    paged = {'ok': True, 'result_ref': 'tr_old', 'chunk_start': 0,
             'content': json.dumps(original)[:500], 'next_cursor': 'cursor'}
    for source in [snapshots, None]:
        rows = messages(['old'])
        rows[1]['content'][0]['content'] = json.dumps(paged)
        compact_recall_messages(rows, ['old'], result_store=source)
        clipped = result(rows, 'old')
        assert clipped['history_truncated']
        assert clipped['result_ref'] == 'tr_old'
        assert 'next_cursor' not in clipped and 'content' not in clipped
        if source is not None:
            assert clipped['episodes'][0]['id'] == 'episode'
        else:
            assert len(clipped['preview']) < 200
