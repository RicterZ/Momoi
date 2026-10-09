#!/usr/bin/env python3
"""Isolated threshold experiment. Reads archived daily outputs; never writes live memories.

Private inputs/results and providers.yaml belong in an ignored --workspace.
Observation routing is retained only to audit event deduplication and counts.
"""
import argparse
import asyncio
import calendar
import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import yaml
from jsonschema import validate

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.models import LLMConfig, ThinkingConfig

TOOL = {
    'name': 'weekly_memory_candidates',
    'description': '提交独立事件分组，供实验程序去重计数；不操作真实记忆。',
    'input_schema': {
        'type': 'object', 'additionalProperties': False, 'required': ['findings'],
        'properties': {'findings': {'type': 'array', 'maxItems': 20, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['key', 'content', 'events', 'conflicts'],
            'properties': {
                'key': {'type': 'string', 'minLength': 1},
                'content': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                'events': {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['observation_ids'],
                    'properties': {'observation_ids': {'type': 'array', 'minItems': 1,
                                   'uniqueItems': True, 'items': {'type': 'string'}}},
                }},
                'conflicts': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
            },
        }}},
    },
}


def month_start(end):
    end = date.fromisoformat(end)
    year, month = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
    return date(year, month, min(end.day, calendar.monthrange(year, month)[1])).isoformat()


def classify(raw, observations, end, previous=()):
    """Compute bounded counts; semantic event independence still needs review."""
    validate(raw, TOOL['input_schema'])
    records = {o['id']: o for o in observations}
    cutoff = (date.fromisoformat(end) - timedelta(days=14)).isoformat()
    month = month_start(end)
    old = {f['key']: f for f in previous}
    proposals = {}
    seen = set()
    for f in raw['findings']:
        key = f['key']
        if not key.strip() or not f['content'].strip() or key in seen:
            raise ValueError('empty_or_duplicate_finding')
        seen.add(key)
        used = set()
        for event in f['events']:
            ids = set(event['observation_ids'])
            if not ids <= records.keys() or ids & used:
                raise ValueError('unknown_or_repeated_observation')
            used |= ids
        conflicts = set(f['conflicts'])
        if not conflicts <= records.keys() or conflicts & used:
            raise ValueError('invalid_conflict')
        proposals[key] = f
    findings, rejected = [], []
    for key, f in proposals.items():
        # A new mention of an old event neither renews its age nor adds support.
        events = [e for e in f['events']
                  if month <= min(records[i]['date'] for i in e['observation_ids']) < end]
        recent = sum(cutoff <= min(records[i]['date'] for i in e['observation_ids']) < end
                     for e in events)
        count = len(events)
        if count < 3 or (key not in old and recent < 3):
            rejected.append({**f, 'events': events, 'count': count, 'reason': 'insufficient_active_support'})
            continue
        status = 'blocked' if f['conflicts'] else 'long_term_candidate' if count >= 8 else 'admission_candidate' if count >= 5 else 'watch'
        findings.append({**f, 'events': events, 'count': count, 'recent_count': recent, 'status': status})
    return {'findings': findings, 'rejected': rejected}


def archive(snapshot, start, end):
    db = sqlite3.connect(f'{Path(snapshot).resolve().as_uri()}?mode=ro', uri=True)
    try:
        rows = db.execute("SELECT local_date, memories_json FROM reflections WHERE state='completed' AND local_date>=? AND local_date<? ORDER BY local_date", (start, end)).fetchall()
    finally:
        db.close()
    expected = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if len(rows) != expected:
        raise ValueError('daily_reflections_missing')
    return [{'id': f'{day}:{n}', 'date': day, **item}
            for day, raw in rows for n, item in enumerate(json.loads(raw), 1)]


async def main(args):
    root = Path(args.workspace)
    root.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load((root / 'providers.yaml').read_text())
    service = config['services']['configured_llm']
    settings = service['settings']
    llm = LLMConfig(settings['base_url'], config['credentials'][service['credentials']]['api_key'],
                    settings['model'], 32768, 0, 300, 0, api_format='openai', thinking=ThinkingConfig(args.effort))
    prompt = Path(__file__).with_suffix('.md').read_text()
    prompt_file = root / 'prompt.md'
    if prompt_file.exists() and prompt_file.read_text() != prompt:
        raise ValueError('use_a_new_workspace_when_changing_the_prompt')
    prompt_file.write_text(prompt)
    end = date.fromisoformat(args.end)
    middle = (end - timedelta(days=7)).isoformat()
    observations = archive(args.snapshot, (end - timedelta(days=14)).isoformat(), args.end)
    (root / 'observations.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2))
    async with OpenAIProvider(llm) as provider:
        async def run(name, data, until, previous=()):
            path = root / f'{name}.json'
            if path.exists():
                return json.loads(path.read_text())
            payload = {'end_exclusive': until, 'previous_candidates': [
                {k: f[k] for k in ('key', 'content', 'events', 'conflicts')} for f in previous
            ], 'observations': data}
            started = time.monotonic()
            response = await provider.complete(prompt, [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}], [TOOL])
            if len(response.tool_calls) != 1 or response.tool_calls[0].name != TOOL['name']:
                raise ValueError('expected_candidate_tool')
            raw = response.tool_calls[0].arguments
            record = {'input': payload, 'raw': raw, 'usage': response.usage,
                      'reasoning': response.reasoning, 'requested_effort': args.effort,
                      'model': settings['model'], 'seconds': time.monotonic() - started}
            try:
                record['result'] = classify(raw, data, until, previous)
            except ValueError as error:
                record['validation_error'] = str(error)
                raise
            finally:
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            print(name, [(f['content'], f['count'], f['status']) for f in record['result']['findings']], flush=True)
            return record

        before = [o for o in observations if o['date'] < middle]
        current = [o for o in observations if o['date'] >= middle]
        async def trial(repeat):
            if args.replay:
                payload = json.loads(Path(args.replay).read_text())['input']
                await run(f'replay-{repeat}', payload['observations'], payload['end_exclusive'], payload['previous_candidates'])
                return
            previous = await run(f'previous-{repeat}', before, middle)
            await run(f'seven-days-{repeat}', current, args.end)
            await run(f'carry-{repeat}', observations, args.end, previous['result']['findings'])

        await asyncio.gather(*(trial(i) for i in range(1, args.repeats + 1)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--end', required=True, help='Exclusive end date; compares the preceding two full weeks')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--effort', choices=['low', 'high'], default='low')
    parser.add_argument('--replay', help='Replay the exact input of a saved result for paired effort comparison')
    asyncio.run(main(parser.parse_args()))
