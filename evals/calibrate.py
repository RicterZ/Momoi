"""Explicit paid judge calibration against editorial synthetic labels."""
import asyncio
import json
import hashlib
from pathlib import Path
import tempfile

from .runner import (ROOT, JUDGE, SOUL, TEMP_ROOT, OpenAIProvider, load_cases,
                     model_config, parse_judgment, write_json)


async def calibrate():
    config = model_config('MOMOI_JUDGE')
    cases = {c['id']: c for c in load_cases()}
    samples = [json.loads(line) for line in (ROOT / 'datasets/judge_calibration.jsonl').read_text(encoding='utf-8').splitlines()]
    output = Path(tempfile.mkdtemp(prefix='momoi-eval-calibration-', dir=TEMP_ROOT))
    write_json(output / 'manifest.json', {
        'model': config.model,
        'parameters': {'temperature': config.temperature, 'max_tokens': config.max_tokens},
        'hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (JUDGE, SOUL, ROOT / 'datasets/interaction.jsonl',
                             ROOT / 'datasets/judge_calibration.jsonl')},
    })
    results = []
    async with OpenAIProvider(config) as judge:
        for sample in samples:
            case = cases[sample['case_id']]
            criterion = sample['criterion']
            payload = {**case, 'criteria': {criterion: case['criteria'][criterion]},
                       'soul': SOUL.read_text(encoding='utf-8'), 'reply': sample['reply']}
            row = {**sample, 'actual_status': 'error'}
            try:
                response = await judge.complete(JUDGE.read_text(encoding='utf-8'),
                                               [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}])
                text = ''.join(b.get('text', '') for b in response.content if b.get('type') == 'text')
                row['checks'] = parse_judgment(text, payload['criteria'])
                row['actual_status'] = row['checks'][0]['status']
            except Exception as error:
                row['error_type'] = type(error).__name__
            results.append(row)
    valid = [r for r in results if r['actual_status'] in {'pass', 'fail'}]
    bad = [r for r in results if r['expected_status'] == 'fail']
    good = [r for r in results if r['expected_status'] == 'pass']
    summary = {'model': config.model, 'label_source': 'editorial_synthetic_not_owner_validated',
               'total': len(results), 'valid': len(valid),
               'agreement': sum(r['expected_status'] == r['actual_status'] for r in valid) / len(valid) if valid else None,
               'missed_violations': sum(r['actual_status'] == 'pass' for r in bad), 'violation_samples': len(bad),
               'false_alarms': sum(r['actual_status'] == 'fail' for r in good), 'good_samples': len(good),
               'errors': sum(r['actual_status'] == 'error' for r in results),
               'insufficient_evidence': sum(r['actual_status'] == 'insufficient_evidence' for r in results)}
    write_json(output / 'summary.json', summary)
    write_json(output / 'results.json', results)
    print(output)
    return int(any(r['expected_status'] != r['actual_status'] for r in results))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(calibrate()))
