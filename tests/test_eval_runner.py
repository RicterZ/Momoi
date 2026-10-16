import asyncio
import json

import pytest

from evals.runner import execute, load_cases, main, model_config, parse_judgment, run_case
from momoi.models import ProviderResponse


def test_synthetic_cases_have_distinct_ids():
    cases = load_cases()
    assert len(cases) == len({c['id'] for c in cases}) == 60
    assert all('虚构测试' in c['input'] for c in cases)


def test_smoke_uses_real_replyer_without_semantic_pass():
    result = asyncio.run(run_case(load_cases()[0], None))
    assert result['status'] == 'unscored'
    assert result['reply']
    assert result['exchanges']
    assert '虚构测试' in json.dumps(result['exchanges'], ensure_ascii=False)
    assert '虚构交互测评' in result['exchanges'][0]['system']


@pytest.mark.parametrize('checks', [[], [
    {'criterion': 'x', 'status': 'pass', 'reason': 'ok'},
    {'criterion': 'x', 'status': 'pass', 'reason': 'ok'},
], [{'criterion': 'x', 'status': 'great', 'reason': 'ok'}]])
def test_invalid_judge_output_rejected(checks):
    with pytest.raises(ValueError):
        parse_judgment(json.dumps({'checks': checks}), {'x': 'criterion'})


class FixedProvider:
    def __init__(self, text):
        self.text = text

    async def complete(self, *args):
        return ProviderResponse([{'type': 'text', 'text': self.text}], [])


def test_judge_pass_fail_and_missing_evidence():
    case = load_cases()[0]
    for status in ('pass', 'fail', 'insufficient_evidence'):
        judge = FixedProvider(json.dumps({'checks': [
            {'criterion': key, 'status': status, 'reason': 'test evidence'} for key in case['criteria']
        ]}))
        result = asyncio.run(run_case(case, FixedProvider('室内拼图'), judge))
        assert result['status'] == status


def test_judge_failure_is_error_and_does_not_persist_exception_message():
    class Broken:
        async def complete(self, *args):
            raise RuntimeError('secret-token')

    result = asyncio.run(run_case(load_cases()[0], FixedProvider('室内拼图'), Broken()))
    assert result['status'] == 'error'
    assert result['error_type'] == 'RuntimeError'
    assert 'secret-token' not in json.dumps(result)


def test_execution_writes_manifest_and_results(tmp_path, monkeypatch):
    from types import SimpleNamespace

    output = tmp_path / 'run'
    output.mkdir()
    import tempfile
    original = tempfile.mkdtemp
    monkeypatch.setattr('evals.runner.tempfile.mkdtemp',
                        lambda *args, **kwargs: str(output) if kwargs.get('prefix') == 'momoi-eval-'
                        else original(*args, **kwargs))
    code = asyncio.run(execute(SimpleNamespace(live=False, judge=False), load_cases()))
    assert code == 0
    manifest = json.loads((output / 'manifest.json').read_text())
    assert manifest['mode'] == 'smoke'
    assert len(manifest['hashes']) == 4
    results = [json.loads(line) for line in (output / 'results.jsonl').read_text().splitlines()]
    assert len(results) == 60
    assert all(r['status'] == 'unscored' for r in results)
    assert json.loads((output / 'summary.json').read_text())['semantic_evaluation'] is False


def test_model_configuration_requires_explicit_eval_environment(monkeypatch):
    for name in ('BASE_URL', 'API_KEY', 'MODEL'):
        monkeypatch.delenv(f'MOMOI_EVAL_{name}', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'unrelated-private-key')
    with pytest.raises(ValueError, match='set MOMOI_EVAL_BASE_URL'):
        model_config('MOMOI_EVAL')
    monkeypatch.setenv('MOMOI_EVAL_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('MOMOI_EVAL_API_KEY', 'synthetic-key')
    monkeypatch.setenv('MOMOI_EVAL_MODEL', 'synthetic-model')
    config = model_config('MOMOI_EVAL')
    assert config.model == 'synthetic-model'
    assert config.api_key == 'synthetic-key'
    assert config.max_retries == 0


@pytest.mark.parametrize('arguments', [['--judge'], ['--case', 'unknown']])
def test_cli_rejects_invalid_options_before_execution(arguments, monkeypatch):
    monkeypatch.setattr('sys.argv', ['evals', *arguments])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_failed_length_check_cannot_be_overruled_by_judge():
    case = load_cases()[0]
    judge = FixedProvider(json.dumps({'checks': [
        {'criterion': key, 'status': 'pass', 'reason': 'test evidence'} for key in case['criteria']
    ]}))
    result = asyncio.run(run_case(case, FixedProvider('长' * (case['max_chars'] + 1)), judge))
    assert result['status'] == 'fail'
    assert any(c['criterion'] == 'length' and c['status'] == 'fail' for c in result['checks'])


def test_history_is_visible_in_real_replyer():
    case = next(c for c in load_cases() if c['id'] == 'history_correction')
    result = asyncio.run(run_case(case, None))
    assert result['status'] == 'unscored'
    messages = json.dumps(result['exchanges'][0]['messages'], ensure_ascii=False)
    assert '演示选绿色' in messages
    assert '改蓝色' in messages
    assert '<history>' in messages


def test_distributions_hide_small_sample_tail_percentiles():
    from evals.report import distribution
    assert distribution([]) == {'n': 0}
    assert 'p95' not in distribution([1, 2, 3])
    assert distribution(list(range(1, 101)))['p95'] == 95
    assert distribution(list(range(1, 101)))['p90'] == 90
    assert 'p50' not in distribution([1, 2], percentiles=False)


def test_report_escapes_untrusted_text_and_pairs_results(tmp_path):
    from evals.report import paired_comparison, render_report, summarize
    result = asyncio.run(run_case(load_cases()[0], None))
    result['reply'] = ['<script>alert(1)</script>']
    result['status'] = 'fail'
    baseline = {**result, 'status': 'pass'}
    assert paired_comparison([result], [baseline])[0]['regression']
    changed = {**baseline, 'case': {**baseline['case'], 'input': 'changed'}}
    assert not paired_comparison([result], [changed])[0]['compatible']
    summary = summarize([result], live=False)
    assert summary['scenario_count'] == 1
    assert summary['counts']['fail'] == 1
    path = tmp_path / 'report.html'
    render_report(path, {'mode': 'smoke'}, summary, [result], [baseline])
    text = path.read_text()
    assert '<script>' not in text
    assert '&lt;script&gt;' in text


def test_calibration_labels_reference_known_criteria():
    from evals.runner import ROOT
    cases = {c['id']: c for c in load_cases()}
    samples = [json.loads(line) for line in (ROOT / 'datasets/judge_calibration.jsonl').read_text().splitlines()]
    assert len(samples) == 12
    for sample in samples:
        assert sample['criterion'] in cases[sample['case_id']]['criteria']
        assert sample['expected_status'] in {'pass', 'fail'}


def test_feature_suite_covers_readme_capabilities_and_voice():
    from evals.runner import ROOT
    cases = load_cases(ROOT / 'datasets/features.jsonl')
    assert len(cases) == 42
    assert {c['tags'][0] for c in cases} == {
        'memory', 'goals', 'heartbeat', 'recovery', 'voice', 'webhook', 'channels_media'}
    voice = next(c for c in cases if c['id'] == 'voice_brief')
    result = asyncio.run(run_case(voice, None))
    assert result['status'] == 'unscored'
    messages = json.dumps(result['exchanges'][0]['messages'], ensure_ascii=False)
    assert '发送形式：语音' in messages
    assert '适合朗读' in messages
    assert all(c['coverage_layer'] == 'replyer_expression_with_supplied_evidence' for c in cases)
