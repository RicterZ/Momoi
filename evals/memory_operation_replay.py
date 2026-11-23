"""Replay synthetic transcripts through the real private memory workflow.

Runs the configured providers in an isolated temporary workspace without starting
the scheduler, channels, MCP or delivery. Records model inputs, tool results
and expectation checks.
"""
import asyncio
from copy import deepcopy
from html import escape
import json
from pathlib import Path
import tempfile
import time

from momoi.channel.onebot import OneBotConfig
from momoi.config.models import AppConfig, HeartbeatConfig
from momoi.models import AgentReply, IncomingMessage, TurnDraft
from momoi.runtime import MomoiDaemon

FIXTURES = Path(__file__).resolve().parents[1] / 'tests/fixtures/memory_operation_transcripts.json'


def verify(case, result):
    expected = case['expected']
    rows = result['active']
    failures = []
    actions = [d['action'] for d in result['decisions']]
    if len(actions) != 1 or actions[0] not in expected['action']:
        failures.append(f'actions {actions}, expected {expected["action"]}')
    if len(rows) != expected['active_count']:
        failures.append(f'active count {len(rows)}, expected {expected["active_count"]}')
    for key in expected.get('unchanged', []):
        if not any(row['id'] == result['seed_ids'][key] for row in rows):
            failures.append(f'original {key} no longer active')
    for key in expected.get('superseded', []):
        if not result['superseded'].get(key):
            failures.append(f'original {key} not superseded')
    if 'tombstone_count' in expected and result['tombstone_count'] != expected['tombstone_count']:
        failures.append('unexpected forget tombstone')
    if expected.get('contains') and not any(expected['contains'] in row['content'] for row in rows):
        failures.append('expected corrected content missing')
    if expected.get('triggers') and not any(row['meta'].get('triggers') == expected['triggers'] for row in rows):
        failures.append('trigger metadata missing')
    if expected.get('searched') and not any(call['name'] == 'memory_operation_search'
            for exchange in result['exchanges'] for call in exchange.get('tool_calls', [])):
        failures.append('no private search issued')
    return failures


async def replay(case, catalog, output):
    output.mkdir(parents=True, exist_ok=False)
    # The temporary workspace prevents dumps or artifacts reaching the real workspace.
    with tempfile.TemporaryDirectory(prefix='momoi-memory-replay-') as directory:
        workspace = Path(directory)
        config = AppConfig(
            providers=catalog, channel=OneBotConfig('ws://127.0.0.1:1', 'test', 1, 60, 30, 30, 20),
            system_prompt='Synthetic replay; no owner agent is run.',
            transcript_turns_min=4, transcript_turns_max=4, episode_unsummarized_tail_turns=2,
            memory_results=8, database=workspace/'memory.sqlite3', workspace=workspace,
            log_level='ERROR', heartbeat=HeartbeatConfig(enabled=False),
            turn_max_seconds=180, turn_max_total_tokens=24000,
        )
        daemon = MomoiDaemon(config)
        store = daemon.store
        exchanges = []
        result = {'name': case['name'], 'fixture': case, 'exchanges': exchanges,
                  'model': catalog.options_for('llm').get('model'), 'seed_ids': {},
                  'retrieval': 'literal' if case.get('literal_only') else 'configured embedding + literal'}
        original_complete = daemon.provider.complete

        async def traced(system, messages, tools=None, **kwargs):
            exchange = {'system': system, 'messages': deepcopy(messages), 'tools': tools}
            exchanges.append(exchange)
            response = await original_complete(system, messages, tools, **kwargs)
            exchange.update(content=response.content, tool_calls=[vars(call) for call in response.tool_calls])
            return response

        daemon.provider.complete = traced
        try:
            now = time.time()
            for index, seed in enumerate(case.get('seeds', [])):
                eid = f'seed:{index}'
                event = IncomingMessage(eid, 'synthetic-owner', seed['content'], now-3600, now-3600)
                store.add_event(event)
                citation = {'event_id': eid, 'quote': event.text}
                mid = store.memories.repository.write({
                    'kind': 'preference', 'key': seed['key'], 'content': seed['content'],
                    'activation': seed.get('activation', 'recall'), 'expires_at': None,
                    'meta': {'tags': [], 'triggers': [], 'scope': seed.get('scope', '')},
                }, [], citation, [citation], now=now-3600)
                result['seed_ids'][seed['key']] = mid
                if seed.get('manual_content'):
                    store.update_memory_content(mid, seed['manual_content'])
                if seed.get('forgotten'):
                    store.memories.repository.forget(store.memories.snapshots([mid])[mid],
                        {'event_id': 'synthetic:forget', 'quote': '忘掉这条记忆'}, now=now-300)
            text = next(row['content'] for row in reversed(case['transcript']) if row['role'] == 'user')
            stamp = now-case.get('event_age', 0)
            event = IncomingMessage('request', 'synthetic-owner', text, stamp, stamp)
            store.add_event(event)
            transcript = [{'role': row['role'], 'content': '<bubble>' + escape(row['content']) + '</bubble>'}
                          for row in case['transcript']]
            operations = [{'id': 'op', **case['request'], 'event_id': event.event_id, 'evidence': event.text}]
            draft = TurnDraft(memory_operations=operations, memory_conversation=transcript,
                memory_context=store.memories.snapshots(list(result['seed_ids'].values())) if case.get('visible') else {})
            store.commit_turn([event], text, AgentReply([]), draft, turn_id='synthetic-owner')
            async with daemon.services:
                if case.get('literal_only'):
                    store.memories.recall.dense_recall = None
                else:
                    daemon.semantic_recall.start()
                    for state in ('building', 'active'):
                        space = store.semantic_space(state=state)
                        if space:
                            store.reconcile_semantic_sources(space['id'])
                    for _ in range(100):
                        if not await daemon.semantic_recall.maintain_once():
                            break
                    if not store.semantic_space(state='active'):
                        raise RuntimeError('synthetic embedding index did not activate')
                batch = store.claim_memory_operation('synthetic-owner')
                await asyncio.wait_for(daemon._run_memory_operation(batch), timeout=200)
            row = store._db.execute("SELECT result_json FROM memory_operation_batches WHERE id='synthetic-owner'").fetchone()
            result['decisions'] = json.loads(row['result_json'])
            result['active'] = store.memories.repository.inventory()
            result['tombstone_count'] = store._db.execute('SELECT COUNT(*) FROM memory_tombstones').fetchone()[0]
            result['superseded'] = {key: store._db.execute('SELECT superseded_by FROM memories WHERE id=?', (mid,)).fetchone()[0]
                                    for key, mid in result['seed_ids'].items()}
            result['failures'] = verify(case, result)
        except Exception as error:
            result.update(error=type(error).__name__ + ': ' + str(error)[:300], failures=['workflow failed'])
        finally:
            store.close()
            (output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str)+'\n')
    print(json.dumps({key: result.get(key) for key in ('name', 'decisions', 'failures', 'error')}, ensure_ascii=False), flush=True)
    return result
