"""Check that weekly verification flags overturned memories and leaves valid ones alone."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.models import ProviderResponse, ToolCall
from momoi.runtime.workflows.memory_verification import (
    MEMORY_VERIFICATION_FINISH_SPEC, PROMPT, memory_verification_input, parse_memory_verification,
)
from momoi.storage import Store
from .runner import TEMP_ROOT, model_config, write_json

DAY = 86400
CASES = [
    {'id': 'overturned', 'memory': '用户喜欢喝冰美式。', 'evidence': '我每天早上都要来一杯冰美式',
     'later': ['最近胃一直不舒服', '医生让我别喝冰的了，我以后不喝冰美式了，改喝热茶'],
     'expected': 'possibly_outdated', 'quote': '不喝冰美式'},
    {'id': 'still_valid', 'memory': '用户不吃香菜。', 'evidence': '我从小就不吃香菜',
     'later': ['帮我点碗牛肉面', '记得备注不要放香菜，我还是受不了那个味'],
     'expected': 'still_valid', 'quote': None},
]


def seed_case(store, case):
    base = time.time() - 40 * DAY
    event_id = f"event:{case['id']}"
    with store.transaction():
        store._db.execute(
            "INSERT INTO events(id,message_id,kind,content,occurred_at,received_at) VALUES (?,?,'message',?,?,?)",
            (event_id, event_id, case['evidence'], base, base),
        )
    source = {'event_id': event_id, 'quote': case['evidence']}
    store.memories.repository.write(
        {'kind': 'preference', 'key': case['id'], 'content': case['memory'], 'activation': 'recall',
         'expires_at': None, 'meta': {'tags': []}}, [], source, [source], now=base)
    for index, (identifier, lines, ids) in enumerate([
        ('source', [case['evidence']], [event_id]), ('later', case['later'], []),
    ]):
        created = base + index * 10 * DAY
        store.create_episode(identifier, episode_id=identifier)
        with store.transaction():
            store._db.execute(
                "UPDATE conversation_episodes SET narrative_summary=?, summarized_through_ordinal=1, created_at=? WHERE id=?",
                (f'{identifier} 话题', created, identifier),
            )
            store._db.execute(
                "INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at) "
                "VALUES (?,'owner','owner','[]','completed',?,?)", (identifier, created, created),
            )
            store._db.execute("INSERT INTO episode_turns(episode_id,turn_id,ordinal,relation) VALUES (?,?,1,'primary')",
                              (identifier, identifier))
            for offset, line in enumerate(lines):
                store._db.execute(
                    "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) VALUES (?,'user',?,?,?)",
                    (identifier, line, created + offset, json.dumps(ids if offset == 0 else [])),
                )
    with store.transaction():
        store._db.execute(
            "INSERT INTO episode_relations(source_episode_id,target_episode_id,relation,explanation,evidence_json,"
            "source_summary_ordinal,created_at,updated_at) VALUES ('later','source','follows_up','同一件事的后续','[]',1,1,1)",
        )


async def evaluate(complete, directory):
    results = []
    for case in CASES:
        store = Store(directory / f"{case['id']}.sqlite3")
        try:
            seed_case(store, case)
            with store.transaction():
                store._queue_memory_verification('eval', time.time())
            run = store.claim_due_memory_verification(type('Config', (), {'enabled': True})())
            source = json.loads(run['input_json'])
            response = await complete(PROMPT, [{'role': 'user', 'content': memory_verification_input(source)}],
                                      [MEMORY_VERIFICATION_FINISH_SPEC])
            call = next((call for call in response.tool_calls if call.name == 'memory_verification_finish'), None)
            row, error = {'id': case['id'], 'expected': case['expected']}, None
            result, error = parse_memory_verification(call.arguments) if call else (None, 'no_tool_call')
            if result is not None:
                store.begin_turn('eval', 'memory_verification', [])
                try:
                    store.commit_memory_verification('eval', 'eval', result)
                except ValueError as failure:
                    error = str(failure)
            finding = result['items'][0] if result and result['items'] else {}
            quotes = [citation['quote'] for citation in finding.get('citations', [])]
            reviews = store.memory_verification_reviews()['items']
            row.update(verdict=finding.get('verdict'), quotes=quotes, error=error, reviews=len(reviews))
            row['passed'] = (error is None and finding.get('verdict') == case['expected']
                             and (case['quote'] is None or any(case['quote'] in quote for quote in quotes))
                             and len(reviews) == (case['expected'] == 'possibly_outdated'))
            results.append(row)
        finally:
            store.close()
    return {'results': results, 'passed': sum(row['passed'] for row in results), 'total': len(CASES)}


async def scripted_complete(system, messages, tools):
    content = messages[0]['content']
    item = json.loads(content.split('<item>')[1].split('</item>')[0].replace('&quot;', '"')
                      .replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&'))
    messages = [message for episode in item['later_episodes'] for message in episode['messages']]
    overturned = next((message for message in messages if '不喝' in message['text']), None)
    cited = overturned or messages[-1]
    call = ToolCall('verify', 'memory_verification_finish', {'items': [{
        'memory_id': item['memory']['id'], 'verdict': 'possibly_outdated' if overturned else 'still_valid',
        'reason': '依据后续对话判断', 'citations': [{'source': cited['id'], 'quote': cited['text']}]}]})
    return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])


async def run(live):
    directory = Path(tempfile.mkdtemp(prefix='momoi-eval-verify-', dir=TEMP_ROOT))
    if live:
        async with OpenAIProvider(model_config('MOMOI_EVAL')) as provider:
            result = await evaluate(provider.complete, directory)
    else:
        result = await evaluate(scripted_complete, directory)
    result['mode'] = 'live' if live else 'scripted_smoke_only'
    write_json(directory / 'result.json', result)
    print(json.dumps({'mode': result['mode'], 'passed': result['passed'], 'total': result['total'],
                      'output': str(directory)}, ensure_ascii=False))
    return int(result['passed'] != result['total'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use explicitly configured MOMOI_EVAL model')
    raise SystemExit(asyncio.run(run(parser.parse_args().live)))
