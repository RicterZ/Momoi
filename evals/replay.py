"""Real storage/workflow replay with synthetic replacements for production shapes."""
import argparse
import asyncio
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.configuration import ProviderBinding, ProviderCatalog
from momoi.integrations.models import LLMConfig
from momoi.models import ProviderResponse, ToolCall
from momoi.storage import Store
from .runner import ROOT, TEMP_ROOT, model_config, write_json

SHAPES = ROOT / 'datasets/production_shapes.json'


def recovery_case(name, directory):
    path = directory / 'state.sqlite3'
    store = Store(path)
    checks = []
    try:
        store.begin_turn('synthetic', 'owner', [])
        if name.startswith('sending_'):
            store.queue_progress('synthetic', 'reply', ['虚构测试通知'], 'dashboard')
            with store._db:
                store._db.execute("UPDATE outbox SET state='sending', attempts=?", (2 if name == 'sending_exhausted' else 1,))
            store.close()
            store = Store(path)
            row = store._db.execute('SELECT state,possible_duplicate FROM outbox').fetchone()
            checks.append(('ambiguous_delivery_not_requeued', tuple(row) ==
                           ('failed' if name == 'sending_exhausted' else 'ambiguous', 1)))
            # Text delivery intentionally permits one bounded ambiguous retry;
            # unlike arbitrary external tools, it tracks possible duplicates.
            checks.append(('bounded_delivery_retry', bool(store.due_outbox()) == (name == 'sending_restart')))
        else:
            capability = 'read' if name == 'read_interrupted' else 'external_effect'
            calls = 0
            result = store.begin_tool_call('synthetic', 'call', 'synthetic_action', {}, capability)
            if result is None:
                calls += 1  # Isolated substitute for an effect, never a real tool.
            if name == 'completed_external':
                store.complete_tool_call('synthetic', 'call', {'ok': True, 'receipt': 'synthetic'})
            if name == 'owner_stop':
                store.cancel_turn('synthetic', reason='owner_stop')
            store.close()  # Inject process loss after dispatch or completion, before turn commit.
            store = Store(path)
            state = store.begin_turn('synthetic', 'owner', [])
            replayed = store.begin_tool_call('synthetic', 'call', 'synthetic_action', {}, capability)
            if replayed is None:
                calls += 1
            expected = 'cancelled' if name == 'owner_stop' else 'running' if capability == 'read' else 'needs_reconciliation'
            checks.extend([('turn_state', state == expected), ('no_duplicate_effect', calls == 1),
                           ('durable_audit', store._db.execute('SELECT COUNT(*) FROM tool_audit').fetchone()[0] == 1)])
            checks.append(('cached_or_ambiguous', replayed == {'ok': True, 'receipt': 'synthetic'}
                           if name == 'completed_external' else bool(replayed and replayed.get('ambiguous'))))
        return {'id': name, 'layer': 'real_storage_fault_injection',
                'checks': [{'criterion': k, 'status': 'pass' if v else 'fail'} for k, v in checks],
                'status': 'pass' if all(v for _, v in checks) else 'fail'}
    finally:
        store.close()


def scripted_finish(case):
    async def complete(self, system, messages, tools=None, **kwargs):
        action = case['expected']['action'][0]
        decision = {'operation_ids': ['op'], 'action': action, 'reason': '虚构场景的脚本化决策'}
        if action in {'write', 'forget'}:
            decision.update(target_ids=[1] if case.get('seeds') else [],
                            evidence=[{'event_id': 'request', 'quote': case['transcript'][-1]['content']}])
        if action == 'write':
            decision['memory'] = {'kind': 'preference', 'key': 'drink', 'content': case['request']['content'],
                                  'activation': 'recall', 'expires_at': None}
        call = ToolCall('finish', 'memory_operation_finish', {'decisions': [decision]})
        return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])
    return complete


async def run(args):
    shapes = json.loads(SHAPES.read_text())
    # Reuse the existing real memory host; no production provider file is loaded.
    spec = importlib.util.spec_from_file_location('momoi_eval_memory_replay', ROOT.parent / 'scripts/replay_memory_operation.py')
    memory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(memory)
    config = model_config('MOMOI_EVAL') if args.live else LLMConfig('http://127.0.0.1:1', 'synthetic', 'scripted', 1024, 0, 1, 0, api_format='openai')
    options = asdict(config)
    options.pop('api_format')
    catalog = ProviderCatalog(Path('/nonexistent/synthetic-providers.yaml'),
                              {'llm': ProviderBinding('chat', 'openai', True, options)})
    output = Path(tempfile.mkdtemp(prefix='momoi-eval-replay-', dir=TEMP_ROOT))
    write_json(output / 'manifest.json', {'mode': 'live_memory' if args.live else 'scripted_memory',
                                         'model': config.model, 'provenance': shapes})
    results = []
    cases = json.loads(memory.FIXTURES.read_text())
    for original in cases:
        if original['name'] not in shapes['memory_cases']:
            continue
        case = {**original, 'literal_only': True, 'visible': True}
        if args.live:
            result = await memory.replay(case, catalog, output / case['name'])
        else:
            with patch.object(OpenAIProvider, 'complete', scripted_finish(case)):
                result = await memory.replay(case, catalog, output / case['name'])
        results.append({'id': case['name'], 'layer': 'real_memory_workflow',
                        'model_mode': 'live' if args.live else 'scripted',
                        'status': 'error' if result.get('error') else 'fail' if result['failures'] else 'pass',
                        'failures': result['failures']})
    for fault in shapes['faults']:
        with tempfile.TemporaryDirectory(prefix='momoi-eval-fault-', dir=TEMP_ROOT) as temp:
            results.append(recovery_case(fault, Path(temp)))
    write_json(output / 'summary.json', results)
    print(output)
    return int(any(r['status'] != 'pass' for r in results))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='explicit paid model calls for memory decisions only')
    return asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
