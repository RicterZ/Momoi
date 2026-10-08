import json
from copy import deepcopy

from momoi.runtime.transcript.native import render_exchanges
from momoi.runtime.turn_support import tool_result_block
from momoi.storage.episode.execution_evidence import eligible
import pytest


def exchange(identifier, name, result):
    return {'content': [{'type': 'tool_use', 'id': identifier, 'name': name,
                         'input': {'path': 'test.txt'}}],
            'results': [{'type': 'tool_result', 'tool_use_id': identifier,
                         'content': json.dumps(result, ensure_ascii=False)}]}


def results(messages):
    return [json.loads(b['content']) for m in messages for b in m['content']
            if b.get('type') == 'tool_result']


@pytest.mark.parametrize('name', ['write_file', 'recall', 'exec'])
@pytest.mark.parametrize('ok', [True, False])
def test_provenance_is_internal_for_live_history_and_recall(name, ok):
    payload = {'ok': ok, 'provenance': {'source': 'builtin', 'tool': name},
               'content': 'evidence', 'url': 'https://example.com/source'}
    original = deepcopy(payload)
    assert 'provenance' not in json.loads(tool_result_block('a', payload)['content'])
    source = exchange('a', name, payload)
    for version in (2, 3):
        replay = results(render_exchanges([source], history_format=version))[0]
        assert 'provenance' not in replay
        assert replay['url'] == payload['url']
    if name != 'recall':
        evidence = eligible(source)['tools'][0]['result']
        assert 'provenance' not in evidence
        assert evidence['url'] == payload['url']
    assert payload == original


def test_history_preview_keeps_edges_reference_and_does_not_mutate_live_result():
    body = '开' * 80 + 'MIDDLE_SECRET' * 100 + '尾' * 80
    source = [exchange('a', 'read_tool_result', {
        'ok': True, 'content': body, 'result_ref': 'tr_original',
        'chunk_start': 100, 'chunk_end': 1500, 'next_cursor': 'cursor',
    })]
    original = deepcopy(source)
    replay = render_exchanges(source)
    result = results(replay)[0]
    assert result['preview'] == '开' * 80 + '\n[...truncated...]\n' + '尾' * 80
    assert result['result_ref'] == 'tr_original'
    assert result['chunk_start'] == 100 and 'next_cursor' not in result
    assert source == original
    assert replay == render_exchanges(source)
    assert replay[0]['content'] == source[0]['content']


def test_error_run_preserves_pairing_distinct_errors_and_stops_at_success():
    source = [exchange(str(i), 'read_file', {
        'ok': False, 'error': error, 'message': 'details', 'result_ref': f'tr_{i}',
    }) for i, error in enumerate(['missing', 'missing', 'denied'])]
    source += [exchange('ok', 'read_file', {'ok': True}),
               exchange('next', 'read_file', {'ok': False, 'error': 'missing'}),
               exchange('other', 'exec', {'ok': False, 'error': 'missing'})]
    replay = render_exchanges(source)
    output = results(replay)
    assert output[0]['error_run_count'] == 3
    assert [e['count'] for e in output[0]['errors']] == [2, 1]
    assert output[1]['error_summary_tool_use_id'] == '0'
    assert output[2]['result_ref'] == 'tr_2'
    assert output[3] == {'ok': True}
    assert output[4]['error'] == output[5]['error'] == 'missing'
    assert len(replay) == len(source) * 2
    for i, item in enumerate(source):
        assert replay[i * 2 + 1]['content'][0]['tool_use_id'] == item['content'][0]['id']


def test_structured_large_result_retains_failure_and_reference():
    source = [exchange('x', 'exec', {
        'ok': False, 'error': 'exit_nonzero', 'result_ref': 'tr_exec',
        'stdout': 'start' + 'x' * 3000 + 'end',
    })]
    result = results(render_exchanges(source))[0]
    assert result['ok'] is False
    assert result['error'] == 'exit_nonzero'
    assert result['result_ref'] == 'tr_exec'
    assert '[...truncated...]' in result['stdout']
    assert result['stdout'].startswith('start') and result['stdout'].endswith('end')
    assert len(result['stdout']) < 200


def test_recall_search_reuse_and_errors_are_never_compacted():
    source = [exchange(str(i), 'recall', payload) for i, payload in enumerate([
        {'ok': True, 'memory': 'evidence' * 1000, 'episodes': 'episode' * 1000},
        {'ok': True, 'status': 'reuse', 'memory': '', 'episodes': ''},
        {'ok': False, 'error': 'invalid_recall', 'message': 'details' * 1000},
        {'ok': False, 'error': 'invalid_recall', 'message': 'details' * 1000},
    ])]
    replay = render_exchanges(source)
    for i, item in enumerate(source):
        assert replay[i * 2 + 1]['content'] == item['results']


def test_simple_receipts_keep_send_pair_but_remove_successful_end_turn():
    source = [exchange('s', 'send_bubbles', {
        'ok': True, 'error': None, 'truncated': False, 'state': 'committed',
        'channel': 'napcat', 'bubbles': 3, 'result_ref': 'tr_send',
        'provenance': {'source': 'runtime'},
    }), exchange('e', 'end_turn', {'ok': True, 'state': 'completed', 'result_ref': 'tr_end'})]
    original = deepcopy(source)
    assert results(render_exchanges(source)) == [
        {'ok': True, 'state': 'committed', 'bubbles': 3}]
    assert results(render_exchanges(source, history_format=2))[0]['result_ref'] == 'tr_send'
    assert source == original


def test_weibo_structured_list_preserves_identity_and_omission_count():
    entries = [{'id': i, 'text': '前' * 80 + '正文' * 400 + '后' * 80,
                'created_at': '2026-09-27', 'user': {'screen_name': '作者'}} for i in range(20)]
    source = [exchange('w', 'mcp__weibo__get_home_timeline', {
        'ok': True, 'result_ref': 'tr_weibo',
        'result': {'content': [], 'structuredContent': {'result': entries}},
    })]
    original = deepcopy(source)
    result = results(render_exchanges(source))[0]
    assert (result['shown'], result['returned_count'], result['omitted']) == (3, 20, 17)
    assert result['items'][0]['id'] == '0'
    assert result['items'][0]['author'] == '作者'
    assert '[...truncated...]' in result['items'][0]['excerpt']
    assert result['result_ref'] == 'tr_weibo'
    assert source == original
    assert 'items' not in results(render_exchanges(source, history_format=2))[0]


def test_file_history_preserves_path_range_and_ref():
    source = [exchange('f', 'read_file', {'ok': True, 'content': 'a' * 2000,
        'start_line': 20, 'end_line': 90, 'result_ref': 'tr_file'})]
    result = results(render_exchanges(source))[0]
    assert result['path'] == 'test.txt'
    assert result['start_line'] == 20 and result['end_line'] == 90
    assert result['result_ref'] == 'tr_file'
    assert result['history_truncated']


def test_real_file_lines_preserve_text_and_continuation():
    payload = {'ok': True, 'path': '/notes.md', 'total_lines': 200,
               'content_offset': 80, 'next_content_offset': 2000, 'truncated': True,
               'sha256': 'abc', 'result_ref': 'tr_file',
               'lines': [{'line': 10, 'text': '开' * 100 + '\n'},
                         {'line': 11, 'text': '中' * 1000 + '\n'},
                         {'line': 12, 'text': '尾' * 100}]}
    result = results(render_exchanges([exchange('f', 'read_file', payload)]))[0]
    assert result['preview'] == '开' * 80 + '\n[...truncated...]\n' + '尾' * 80
    assert (result['start_line'], result['end_line']) == (10, 12)
    assert result['next_content_offset'] == 2000 and result['truncated']
    assert 'sha256' not in result


def test_non_feed_list_does_not_drop_unknown_business_fields():
    payload = {'ok': True, 'result_ref': 'tr_inventory',
               'items': [{'id': 'item', 'name': '药品', 'quantity': 0,
                          'warnings': '重要警告' * 300}]}
    result = results(render_exchanges([exchange('i', 'inventory', payload)]))[0]
    assert 'items' not in result
    assert 'preview' in result and result['result_ref'] == 'tr_inventory'


def test_exec_preserves_exit_and_both_output_streams():
    source = [exchange('x', 'exec', {'ok': False, 'exit_code': 2,
        'stdout_tail': 'output' * 300, 'stderr_tail': 'failure' * 300, 'result_ref': 'tr_exec'})]
    result = results(render_exchanges(source))[0]
    assert result['exit_code'] == 2 and result['ok'] is False
    assert result['stdout_tail'].startswith('output')
    assert result['stderr_tail'].startswith('failure')
    assert result['result_ref'] == 'tr_exec'


def test_error_run_does_not_merge_distinct_exec_failures():
    source = [exchange(str(i), 'exec', {'ok': False, 'exit_code': i,
        'stderr_tail': message, 'result_ref': str(i)})
        for i, message in [(1, 'permission denied'), (2, 'syntax error')]]
    result = results(render_exchanges(source))[0]
    assert len(result['errors']) == 2
    assert 'permission denied' in result['errors'][0]['detail']
    assert 'syntax error' in result['errors'][1]['detail']


def test_large_web_and_plan_results_keep_outcome_metadata():
    for name, fields in [('web_fetch', {'status': 404, 'requested_url': 'https://example.org',
                                      'content_type': 'text/html', 'source_truncated': True}),
                         ('plan_get', {'plan_id': 'p', 'status': 'paused', 'step_index': 2}),
                         ('mcp__bgmi__update', {'ambiguous': True, 'upstream_error_type': 'TimeoutError'})]:
        payload = {'ok': False, 'content': 'large' * 300, 'result_ref': 'tr_x', **fields}
        result = results(render_exchanges([exchange('x', name, payload)]))[0]
        for key, value in fields.items():
            assert result[key] == value


def test_paged_history_omits_hash_cursor_and_previews_content_not_envelope():
    source = [exchange('p', 'read_tool_result', {
        'ok': True, 'result_ref': 'tr_example', 'sha256': 'a' * 64,
        'chunk_start': 0, 'chunk_end': 2000, 'next_cursor': 'encoded-cursor',
        'has_more': True, 'content': '正文' * 1000,
    })]
    result = results(render_exchanges(source))[0]
    assert 'sha256' not in result and 'next_cursor' not in result
    assert result['result_ref'] == 'tr_example'
    assert 'tr_example' not in result['preview']
    assert '正文' in result['preview']


def test_wrapped_mcp_preview_excludes_duplicate_outer_reference():
    source = [exchange('m', 'mcp__example__read', {
        'ok': True, 'result_ref': 'tr_example',
        'result': {'content': [{'type': 'text', 'text': '正文' * 1000}]},
    })]
    result = results(render_exchanges(source))[0]
    assert result['result_ref'] == 'tr_example'
    assert 'tr_example' not in result['preview']
    assert '正文' in result['preview']


def test_existing_recall_observation_drops_nested_hash_without_clipping_evidence():
    body = '原文证据' * 1000
    source = [exchange('r', 'recall', {'ok': True, 'episodes': [{'turns': [
        {'execution': [{'tools': [{'name': 'read_file', 'result': {'sha256': 'hash', 'content': body}}]}]}
    ]}]})]
    original = deepcopy(source)
    result = results(render_exchanges(source))[0]
    evidence = result['episodes'][0]['turns'][0]['execution'][0]['tools'][0]['result']
    assert evidence == {'content': body}
    assert source == original


def test_mcp_preview_uses_text_result_without_outer_ref():
    source = [exchange('m', 'mcp__example__read', {
        'ok': True, 'result_ref': 'tr_example',
        'result': {'content': [{'type': 'text', 'text': '开始' * 80 + '正文' * 1000 + '结束' * 80}], 'isError': False},
    })]
    result = results(render_exchanges(source))[0]
    assert result['preview'].startswith('开始') and result['preview'].endswith('结束')
    assert 'tr_example' not in result['preview'] and '"content"' not in result['preview']


def test_partial_snapshot_preview_uses_saved_result_not_incomplete_json(tmp_path):
    from momoi.runtime.agent.result_store import ToolResultStore
    snapshots = ToolResultStore(tmp_path / 'results')
    text = '开始' * 80 + '正文' * 2000 + '结束' * 80
    ref = snapshots.save(json.dumps({'ok': True, 'result': {'content': [{'type': 'text', 'text': text}], 'isError': False}}, ensure_ascii=False))
    chunk = snapshots.read(ref, None, max_chars=1000, provenance={})
    original = deepcopy(chunk)
    source = [exchange('m', 'mcp__example__read', chunk)]
    result = results(render_exchanges(source, result_store=snapshots))[0]
    assert result['preview'].startswith('开始') and result['preview'].endswith('结束')
    assert ref not in result['preview'] and '"result"' not in result['preview']
    assert chunk == original


def test_unstructured_recall_observation_remains_verbatim():
    source = [exchange('r', 'recall', {})]
    source[0]['results'][0]['content'] = '历史原文观察'
    replay = render_exchanges(source)
    assert replay[1]['content'][0]['content'] == '历史原文观察'
