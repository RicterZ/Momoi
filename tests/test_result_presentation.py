import json
from copy import deepcopy
from types import SimpleNamespace

from momoi.models import ToolCall
from momoi.tools.presentation import mcp_body, present_result
from momoi.runtime.agent.result_store import ToolResultStore
from momoi.runtime.agent.tool_executor import ToolExecutor
from momoi.runtime.turn_support import tool_result_block
from momoi.runtime.transcript.native import render_exchanges
from momoi.storage.episode.execution_evidence import historical_result


def test_mcp_projection_matches_live_history_and_execution_without_changing_raw(tmp_path):
    raw = {'ok': True, 'error': None, 'truncated': False, 'result': {
        'content': [{'type': 'text', 'text': json.dumps({'quantity': 0, 'updated': False})}],
        'isError': False}}
    original = deepcopy(raw)
    snapshots = ToolResultStore(tmp_path / 'results')
    executor = ToolExecutor(SimpleNamespace(workspace=tmp_path, database=tmp_path / 'db',
                           tool_result_max_chars=12000), None, None, None, None, snapshots)
    result = executor.normalize(ToolCall('c', 'mcp__stock__read', {}), raw, 'mcp')
    live = json.loads(tool_result_block('c', result)['content'])
    assert live['result'] == {'quantity': 0, 'updated': False}
    assert 'error' not in live and 'truncated' not in live
    assert snapshots.historical_payload(result['result_ref'])['result'] == raw['result']
    exchange = {'content': [{'type': 'tool_use', 'id': 'c', 'name': 'mcp__stock__read', 'input': {}}],
                'results': [tool_result_block('c', result)]}
    history = render_exchanges([exchange])[-1]['content'][0]
    assert json.loads(history['content']) == live
    assert historical_result(result) == live
    assert raw == original


def test_mcp_plain_text_invalid_json_and_mixed_media_are_preserved():
    for text in ['hello', '{"broken":', '42']:
        assert mcp_body({'content': [{'type': 'text', 'text': text}]}) == text
    blocks = [{'type': 'text', 'text': '{"a": 1}'}, {'type': 'image', 'data': 'binary'}]
    assert mcp_body({'content': blocks}) == {'content': blocks}
    assert mcp_body({'content': [{'type': 'text', 'text': 'one'},
                                 {'type': 'text', 'text': 'two'}]}) == ['one', 'two']
    business = {'content': 'description', 'quantity': 0, 'status': 'accepted'}
    assert present_result({'ok': True, 'result': business})['result'] == business


def test_large_mcp_write_preserves_pending_outcome_and_business_values(tmp_path):
    from momoi.tools.presentation import fit_result
    snapshots = ToolResultStore(tmp_path / 'results')
    executor = ToolExecutor(SimpleNamespace(workspace=tmp_path, database=tmp_path / 'db',
                           tool_result_max_chars=1000), None, None, None, None, snapshots)
    business = {'description': 'large ' * 4000, 'quantity': 0, 'changed': False,
                'status': 'accepted', 'operation_id': 'op-123', 'next_cursor': 'business-page-2'}
    raw = {'ok': True, 'result': {'content': [{'type': 'text', 'text': json.dumps(business)}]}}
    result = executor.normalize(ToolCall('c', 'mcp__stock__update', {}), raw, 'mcp')
    live = json.loads(tool_result_block('c', result)['content'])
    assert len(json.dumps(live, ensure_ascii=False)) <= 1000
    assert live['result']['quantity'] == 0 and live['result']['changed'] is False
    assert live['result']['status'] == 'accepted'
    assert live['result']['operation_id'] == 'op-123'
    assert live['result']['next_cursor'] == 'business-page-2'
    assert live['truncated'] and live['omitted_fields']
    assert json.dumps(live).count(result['result_ref']) == 1
    assert snapshots.historical_payload(result['result_ref'])['result'] == raw['result']
    historical = fit_result(present_result(raw), 800, string_limit=160)
    assert historical['result']['quantity'] == 0
    assert historical['result']['status'] == 'accepted'


def test_structured_history_stays_fixed_as_later_turns_arrive():
    from momoi.runtime.transcript.native import render_exchanges
    source = {'content': [{'type': 'tool_use', 'id': 'a', 'name': 'inventory', 'input': {}}],
              'results': [tool_result_block('a', {'ok': True, 'state': 'accepted', 'result_ref': 'tr_inventory',
                  'items': [{'quantity': 0, 'operation_id': 'op', 'notes': 'long ' * 3000}]})]}
    original = deepcopy(source)
    prefix = render_exchanges([source])
    later = {'content': [{'type': 'tool_use', 'id': 'b', 'name': 'inventory', 'input': {}}],
             'results': [tool_result_block('b', {'ok': True, 'state': 'committed'})]}
    assert render_exchanges([source, later])[:len(prefix)] == prefix
    assert render_exchanges([source]) == prefix
    result = json.loads(prefix[-1]['content'][0]['content'])
    assert result['state'] == 'accepted'
    assert result['items'][0]['quantity'] == 0
    assert result['items'][0]['operation_id'] == 'op'
    assert source == original


def test_reply_display_omits_success_receipt_but_preserves_internal_outcome():
    from momoi.runtime.turn_support import tool_result_block
    from momoi.tools.presentation import present_result
    payload = {'ok': True, 'state': 'committed', 'bubbles': ['hello'],
               'result_ref': 'tr_reply', 'provenance': {'tool': 'reply'}}
    shown = json.loads(tool_result_block('r', payload)['content'])
    assert shown == {'bubbles': ['hello'], 'result_ref': 'tr_reply'}
    assert present_result(payload)['ok'] is True
    assert not tool_result_block('r', payload)['is_error']
    failed = {**payload, 'ok': False, 'error': 'send_failed'}
    assert json.loads(tool_result_block('r', failed)['content'])['error'] == 'send_failed'
    delivery = {**payload, 'delivery_state': 'failed'}
    assert present_result(delivery, historical=True)['delivery_state'] == 'failed'
