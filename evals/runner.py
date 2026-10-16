"""Small, opt-in runner around the production Replyer, without private config."""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from time import monotonic
from types import SimpleNamespace

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime.agent.replyer import Replyer
from momoi.storage import Store

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / 'datasets/interaction.jsonl'
SOUL = ROOT / 'prompts/soul.md'
JUDGE = ROOT / 'prompts/judge.md'
TEMP_ROOT = None if os.name == 'nt' else '/tmp'


def load_cases(dataset=DATASET):
    cases = [json.loads(line) for line in dataset.read_text(encoding='utf-8').splitlines() if line.strip()]
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or case['id'] in ids:
            raise ValueError('invalid or duplicate case id')
        ids.add(case['id'])
        for field in ('input', 'intent', 'reference'):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f'{case["id"]}: invalid {field}')
        if not isinstance(case.get('criteria'), dict) or not case['criteria']:
            raise ValueError('criteria must be a nonempty object')
        if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in case['criteria'].items()):
            raise ValueError('invalid criterion')
        if type(case.get('max_chars')) is not int or case['max_chars'] <= 0:
            raise ValueError('invalid max_chars')
        if case.get('split') not in {'dev', 'holdout', 'regression'}:
            raise ValueError('invalid split')
        if not isinstance(case.get('history'), list) or not all(
            isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ('user', 'assistant'))
            for row in case['history']
        ):
            raise ValueError('invalid history')
        expected = case.get('expected', {})
        if not isinstance(expected, dict) or not all(isinstance(expected.get(k), list) and all(isinstance(v, str) for v in expected[k])
                   for k in ('must_convey', 'must_not_convey')):
            raise ValueError('invalid expected semantics')
        if not isinstance(case.get('tags'), list) or not case['tags'] or not all(isinstance(t, str) for t in case['tags']):
            raise ValueError('invalid tags')
        if case.get('mode', 'text') not in {'text', 'voice'}:
            raise ValueError('invalid mode')
    return cases


def checks_for(case, bubbles):
    text = '\n\n'.join(bubbles)
    return [
        {'criterion': 'nonempty', 'status': 'pass' if text.strip() else 'fail', 'reason': 'nonempty output'},
        {'criterion': 'length', 'status': 'pass' if len(text) <= case['max_chars'] else 'fail',
         'reason': f'{len(text)}/{case["max_chars"]} characters'},
    ]


def parse_judgment(text, criteria):
    value = json.loads(text)
    checks = value.get('checks') if isinstance(value, dict) else None
    if not isinstance(checks, list) or len(checks) != len(criteria):
        raise ValueError('judge must return one check per criterion')
    names = []
    for check in checks:
        if not isinstance(check, dict) or check.get('status') not in {'pass', 'fail', 'insufficient_evidence'}:
            raise ValueError('invalid judge status')
        if not isinstance(check.get('reason'), str) or not check['reason'].strip():
            raise ValueError('missing judge reason')
        names.append(check.get('criterion'))
    if set(names) != set(criteria) or len(set(names)) != len(names):
        raise ValueError('judge criterion mismatch')
    return checks


class RecordedProvider:
    def __init__(self, delegate=None):
        self.delegate = delegate
        self.exchanges = []

    async def complete(self, system, messages, tools=None):
        exchange = {'system': system, 'messages': deepcopy(messages), 'tools': tools}
        self.exchanges.append(exchange)
        started = monotonic()
        try:
            response = (await self.delegate.complete(system, messages, tools) if self.delegate else
                        ProviderResponse([{'type': 'text', 'text': '离线占位回复：仅验证执行链路。'}], []))
        finally:
            exchange['duration_ms'] = round((monotonic() - started) * 1000, 3)
        # Do not persist reasoning or protocol continuation data.
        exchange.update(text=[b['text'] for b in response.content if b.get('type') == 'text'],
                        tool_calls=[{'name': c.name, 'arguments': c.arguments} for c in response.tool_calls],
                        usage=response.usage)
        return response


async def run_case(case, provider, judge=None):
    started = monotonic()
    result = {'id': case['id'], 'case': case, 'checks': [], 'status': 'error'}
    recorded = RecordedProvider(provider)
    try:
        with tempfile.TemporaryDirectory(prefix='momoi-eval-case-', dir=TEMP_ROOT) as directory:
            store = Store(Path(directory) / 'state.sqlite3')
            try:
                for index, row in enumerate(case.get('history', [])):
                    timestamp = 1700000000 - 60 * (len(case['history']) - index)
                    historical = IncomingMessage(f'history-{index}', f'history-{index}', row['user'],
                                                 timestamp, timestamp, channel='dashboard')
                    store.add_event(historical)
                    store.begin_turn(f'history-{index}', 'owner', [historical.event_id])
                    store.commit_turn([historical], row['user'], AgentReply([row['assistant']]),
                                      turn_id=f'history-{index}', target_channel='dashboard')
                    # Seed synthetic delivered history without contacting a channel.
                    with store._db:
                        store._db.execute("UPDATE messages SET delivery_state='delivered', created_at=? WHERE turn_id=? AND role='assistant'",
                                          (timestamp + 1, f'history-{index}'))
                event = IncomingMessage('input', 'synthetic', case['input'], 1700000000, 1700000000,
                                        channel='dashboard')
                store.add_event(event)
                store.begin_turn('eval', 'owner', [event.event_id])
                config = SimpleNamespace(soul_prompt_path=None, soul_prompt=SOUL.read_text(encoding='utf-8'),
                                         timezone='UTC', thinking_stages={})
                request = SimpleNamespace(turn_id='eval', round_number=1, current_events=[event],
                                          delivery_channel=SimpleNamespace(name='dashboard'))
                bubbles = await Replyer(config, store, recorded).generate(
                    ToolCall('reply', 'reply', {'intent': case['intent'], 'reference': case['reference'],
                                              'mode': case.get('mode', 'text')}), request)
            finally:
                store.close()
        result['reply'] = bubbles
        result['checks'] = checks_for(case, bubbles)
        if judge:
            payload = {**case, 'soul': SOUL.read_text(encoding='utf-8'), 'reply': bubbles}
            judge_started = monotonic()
            try:
                response = await judge.complete(JUDGE.read_text(encoding='utf-8'),
                                               [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}])
            finally:
                result['judge_ms'] = round((monotonic() - judge_started) * 1000, 3)
            result['judge_usage'] = response.usage
            text = ''.join(b.get('text', '') for b in response.content if b.get('type') == 'text')
            result['judge_output'] = text
            result['checks'].extend(parse_judgment(text, case['criteria']))
        statuses = {c['status'] for c in result['checks']}
        result['status'] = ('fail' if 'fail' in statuses else 'insufficient_evidence'
                            if 'insufficient_evidence' in statuses else 'pass' if judge else 'unscored')
    except Exception as error:
        # Exception messages may contain HTTP credentials; retain the type only.
        result['error_type'] = type(error).__name__
        result['error_stage'] = 'judge' if 'reply' in result else 'replyer'
    result['replyer_ms'] = sum(e['duration_ms'] for e in recorded.exchanges)
    result.update(exchanges=recorded.exchanges, duration_ms=int((monotonic() - started) * 1000))
    return result


def model_config(prefix):
    values = {name: os.environ.get(f'{prefix}_{name}', '') for name in ('BASE_URL', 'API_KEY', 'MODEL')}
    if not all(values.values()):
        raise ValueError(f'set {prefix}_BASE_URL, {prefix}_API_KEY and {prefix}_MODEL explicitly')
    return LLMConfig(values['BASE_URL'], values['API_KEY'], values['MODEL'], 1024, 0, 60, 0,
                     api_format='openai')


def git_info(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT.parent, text=True).strip()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


async def execute(args, cases):
    from contextlib import AsyncExitStack

    config = model_config('MOMOI_EVAL') if args.live else None
    judge_config = model_config('MOMOI_JUDGE') if args.judge else None
    dataset = ROOT / 'datasets' / (getattr(args, 'suite', 'interaction') + '.jsonl')
    output = Path(tempfile.mkdtemp(prefix='momoi-eval-', dir=TEMP_ROOT))
    manifest = {'mode': 'live' if args.live else 'smoke', 'commit': git_info('rev-parse', 'HEAD'),
                'dirty': bool(git_info('status', '--porcelain')), 'case_ids': [c['id'] for c in cases],
                'repeat': getattr(args, 'repeat', 1),
                'suite': getattr(args, 'suite', 'interaction'),
                'coverage_layer': 'replyer_expression_with_supplied_evidence',
                'model': config.model if config else None, 'judge_model': judge_config.model if judge_config else None,
                'parameters': {'temperature': 0, 'max_tokens': 1024, 'timeout_seconds': 60, 'max_retries': 0},
                'hashes': {str(p.relative_to(ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (dataset, SOUL, JUDGE, ROOT.parent / 'src/momoi/prompts/replyer.md')}}
    write_json(output / 'manifest.json', manifest)
    results = []
    async with AsyncExitStack() as stack:
        provider = await stack.enter_async_context(OpenAIProvider(config)) if config else None
        judge = await stack.enter_async_context(OpenAIProvider(judge_config)) if judge_config else None
        with (output / 'results.jsonl').open('w', encoding='utf-8') as handle:
            for case in cases:
                for repetition in range(getattr(args, 'repeat', 1)):
                    result = await run_case(case, provider, judge)
                    result['repetition'] = repetition + 1
                    results.append(result)
                    handle.write(json.dumps(result, ensure_ascii=False) + '\n')
                    handle.flush()
    counts = {status: sum(r['status'] == status for r in results)
              for status in ('pass', 'fail', 'error', 'insufficient_evidence', 'unscored')}
    from .report import summarize, render_report
    summary = summarize(results, live=args.live)
    summary['semantic_evaluation'] = bool(judge_config)
    write_json(output / 'summary.json', summary)
    baseline = getattr(args, 'baseline', None)
    previous = None
    if baseline:
        previous = [json.loads(line) for line in (baseline / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
    render_report(output / 'report.html', manifest, summary, results, previous)
    print(f'{output}\n{json.dumps(counts)}')
    return int(bool(counts['fail'] or counts['error'] or counts['insufficient_evidence']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='explicitly call an external model')
    parser.add_argument('--judge', action='store_true', help='explicitly call a separate judge (requires --live)')
    parser.add_argument('--case', action='append', default=[], help='select a case id; repeatable')
    parser.add_argument('--suite', choices=['interaction', 'features'], default='interaction')
    parser.add_argument('--split', choices=['dev', 'holdout', 'regression'])
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--baseline', type=Path, help='previous output directory for paired comparison')
    args = parser.parse_args()
    if args.judge and not args.live:
        parser.error('--judge requires --live')
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    if args.baseline and not (args.baseline / 'results.jsonl').is_file():
        parser.error('baseline must contain results.jsonl')
    cases = load_cases(ROOT / 'datasets' / (args.suite + '.jsonl'))
    unknown = set(args.case) - {c['id'] for c in cases}
    if unknown:
        parser.error('unknown case id')
    if args.case:
        cases = [c for c in cases if c['id'] in args.case]
    if args.split:
        cases = [c for c in cases if c['split'] == args.split]
    if not cases:
        parser.error('no matching cases')
    try:
        return asyncio.run(execute(args, cases))
    except ValueError as error:
        parser.error(str(error))
