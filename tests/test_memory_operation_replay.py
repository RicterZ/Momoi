"""Replay harness checks use scripted model outputs; live runs are explicit CLI only."""
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('memory_replay', Path(__file__).resolve().parents[1] / 'scripts/replay_memory_operation.py')
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def test_transcripts_cover_distinct_write_boundaries():
    cases = json.loads(replay.FIXTURES.read_text())
    assert len(cases) == len({case['name'] for case in cases}) == 14
    for case in cases:
        assert case['transcript'] and any(row['role'] == 'user' for row in case['transcript'])
        assert case['request']['type'] in {'add', 'replace', 'forget'}
        assert 'scope' in case['request'] and case['expected']['action']


def test_expectations_detect_wrong_action_duplicate_stale_version_and_missing_search():
    case = {'expected': {'action': ['write'], 'active_count': 1, 'superseded': ['drink'],
                        'contains': '无糖', 'searched': True}}
    result = {'decisions': [{'action': 'noop'}], 'active': [{'id': 1, 'content': '甜咖啡'}],
              'seed_ids': {'drink': 1}, 'superseded': {'drink': None}, 'exchanges': []}
    assert len(replay.verify(case, result)) == 4
    result.update(decisions=[{'action': 'write'}], active=[{'id': 2, 'content': '无糖咖啡'}],
                  superseded={'drink': 2}, exchanges=[{'tool_calls': [{'name': 'memory_operation_search'}]}])
    assert replay.verify(case, result) == []
    result['active'].append({'id': 3, 'content': '多余重复'})
    assert len(replay.verify(case, result)) == 1


def test_replay_runs_real_host_with_scripted_provider_and_only_temporary_database(tmp_path, monkeypatch):
    import asyncio
    from momoi.integrations.adapters.openai import OpenAIProvider
    from momoi.integrations.models import LLMConfig
    from momoi.models import ProviderResponse, ToolCall
    from tests.support import provider_catalog

    async def complete(self, system, messages, tools=None, **kwargs):
        assert 'OWNER' in messages[0]['content']
        assert 'operation_requests' in messages[1]['content'][0]['text']
        call = ToolCall('finish', 'memory_operation_finish', {'decisions': [{
            'operation_ids': ['op'], 'action': 'noop', 'reason': '一次经历，不记录长期记忆',
        }]})
        return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])

    monkeypatch.setattr(OpenAIProvider, 'complete', complete)
    case = next(case for case in json.loads(replay.FIXTURES.read_text()) if case['name'] == 'single_episode')
    case['literal_only'] = True
    catalog = provider_catalog(LLMConfig('http://127.0.0.1:1', 'test', 'test', 1000, 0, 1, 0, api_format='openai'))
    result = asyncio.run(replay.replay(case, catalog, tmp_path/'result'))
    assert result['failures'] == []
    assert len(result['exchanges']) == 1
    assert json.loads((tmp_path/'result/result.json').read_text())['decisions'][0]['action'] == 'noop'
    assert not list(tmp_path.rglob('*.sqlite3'))
