import json
from types import SimpleNamespace

from momoi.models import ToolCall
from momoi.runtime.agent.tool_executor import ToolExecutor
from momoi.runtime.agent.result_store import ToolResultStore
from momoi.runtime.turn_support import tool_result_block
from momoi.tools.presentation import present_result


def normalized(tmp_path, name, raw):
    snapshots = ToolResultStore(tmp_path / 'results')
    executor = ToolExecutor(SimpleNamespace(workspace=tmp_path, database=tmp_path / 'db',
                           tool_result_max_chars=12000), None, None, None, None, snapshots)
    result = executor.normalize(ToolCall('c', name, {}), raw, 'builtin')
    return json.loads(tool_result_block('c', result)['content']), snapshots


def test_memory_search_hides_ranking_without_losing_source_or_snapshot(tmp_path):
    item = {'id': 1, 'kind': 'preference', 'content': 'fact', 'source': 'reflection',
            'confidence': .5, 'evidence': 'quote', 'updated_at': 10,
            'search_score': .8, 'dense_cosine': .9, 'unit_ids': ['u']}
    shown, snapshots = normalized(tmp_path, 'memory_search', {'ok': True, 'results': [item]})
    assert shown['results'] == [{k: v for k, v in item.items() if k not in {'search_score', 'dense_cosine', 'unit_ids'}}]
    assert snapshots.historical_payload(shown['result_ref'])['results'] == [item]
    refitted = json.loads(snapshots.refit(json.dumps(shown), max_chars=1000))
    assert 'dense_cosine' not in refitted['results'][0]
    assert present_result(shown, tool_name='memory_search') == shown
