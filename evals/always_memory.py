"""Compare real Replyer adherence before and after a reviewed always-budget reduction."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime.agent.replyer import Replyer
from momoi.storage import Store
from .runner import TEMP_ROOT, model_config, write_json

CASES = [
    {'id': 'address', 'memory': '用户要求每次称呼他为阿澈。', 'input': '早呀，叫我一声再打个招呼。',
     'expected': ['阿澈']},
    {'id': 'diet', 'memory': '用户不吃香菜，帮用户点餐时必须明确备注不加香菜。',
     'input': '帮我写一句牛肉面的下单备注。', 'expected': ['不加香菜', '不放香菜', '不要香菜', '去香菜']},
]


async def evaluate_always(complete, directory):
    results = []
    for case in CASES:
        for mode in ('before', 'after'):
            store = Store(directory / f"{case['id']}-{mode}.sqlite3")
            try:
                store.memories.repository.always_token_budget = 100000
                def seed(key, content):
                    source = {'event_id': 'synthetic', 'quote': content}
                    return store.memories.repository.write(
                        {'kind': 'preference', 'key': key, 'content': content, 'activation': 'always',
                         'expires_at': None, 'meta': {'tags': []}}, [], source, [source], now=100)
                seed('relevant', case['memory'])
                for index in range(24):
                    seed(f'background_{index}', f'用户在第 {index} 个虚构游戏中偏好蓝色装饰。' * 5)
                baseline_tokens = store.always_memory_tokens()
                if mode == 'after':
                    # Synthetic complete observation period with no recorded use.
                    with store.transaction():
                        store._db.execute('UPDATE memory_usage_observation SET started_at=?',
                                          (time.time() - 61 * 86400,))
                    store.generate_memory_activation_suggestions()
                    for item in store.memory_activation_suggestions()['items']:
                        if item['key'].startswith('background_'):
                            store.review_memory_activation_suggestion(item['id'], approve=True)
                    store.memories.repository.always_token_budget = 1500
                    store.memories.repository.check_always_budget()
                event = IncomingMessage('input', 'synthetic', case['input'], 1700000000, 1700000000, channel='dashboard')
                store.add_event(event)
                store.begin_turn('eval', 'owner', ['input'])
                config = SimpleNamespace(soul_prompt_path=None, soul_prompt='你是用户的中文助手，请遵守用户的称呼和饮食限制。',
                                         timezone='UTC', thinking_stages={})
                request = SimpleNamespace(turn_id='eval', round_number=1, current_events=[event],
                                          delivery_channel=SimpleNamespace(name='dashboard'))
                bubbles = await Replyer(config, store, SimpleNamespace(complete=complete)).generate(
                    ToolCall('reply', 'reply', {'intent': case['input'], 'reference': ''}), request)
                text = '\n'.join(bubbles)
                results.append({'id': case['id'], 'mode': mode, 'always_tokens': store.always_memory_tokens(),
                                'baseline_tokens': baseline_tokens, 'reply': text,
                                'adhered': any(word in text for word in case['expected'])})
            finally:
                store.close()
    return {'results': results, 'adherence': {
        mode: sum(row['adhered'] for row in results if row['mode'] == mode) / len(CASES)
        for mode in ('before', 'after')}}


async def scripted_complete(system, messages, tools):
    prompt = str(messages)
    assert 'key="relevant"' in prompt
    text = '阿澈，早呀。' if '叫我一声' in prompt else '牛肉面备注：不加香菜。'
    return ProviderResponse([{'type': 'text', 'text': text}], [])


async def run(live):
    directory = Path(tempfile.mkdtemp(prefix='momoi-eval-always-', dir=TEMP_ROOT))
    if live:
        async with OpenAIProvider(model_config('MOMOI_EVAL')) as provider:
            result = await evaluate_always(provider.complete, directory)
    else:
        result = await evaluate_always(scripted_complete, directory)
    result['mode'] = 'live' if live else 'scripted_smoke_only'
    write_json(directory / 'result.json', result)
    print(json.dumps({'mode': result['mode'], 'adherence': result['adherence'], 'output': str(directory)}, ensure_ascii=False))
    return int(any(not row['adhered'] for row in result['results']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use explicitly configured MOMOI_EVAL model')
    raise SystemExit(asyncio.run(run(parser.parse_args().live)))
