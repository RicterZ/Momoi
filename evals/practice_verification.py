"""Compare the next action for verified/unverified methods; never execute external work."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.memory.text import format_memory
from momoi.models import ProviderResponse, ToolCall
from momoi.storage import Store
from momoi.tools.contracts.memory import MEMORY_TOOL_POLICY
from .runner import TEMP_ROOT, model_config, write_json

CASES = [
    ('config', '重启演示服务前，运行 config_check，检查通过后重启。', '按记住的方法重启演示服务。'),
    ('backup', '备份演示笔记时，先调用 export_notes 再校验导出文件。', '按记住的方法备份演示笔记。'),
]
TOOLS = [
    {'name': name, 'description': description,
     'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
    for name, description in [('confirm_method', '向用户确认是否采用记忆中的方法。'),
                              ('execute_method', '开始按记忆中的方法执行任务。')]
]


async def evaluate_practice(complete, directory):
    results = []
    for key, content, task in CASES:
        for status in ('verified', 'unverified'):
            store = Store(directory / f'{key}-{status}.sqlite3')
            try:
                source = {'event_id': 'synthetic', 'quote': content}
                meta = {'tags': [], 'verification': status, 'pitfalls': ['仅用于演示环境。']}
                if status == 'verified':
                    meta['basis'] = 'execution:synthetic:successful-check'
                identifier = store.memories.repository.write(
                    {'kind': 'practice', 'key': key, 'content': content, 'activation': 'recall',
                     'expires_at': None, 'meta': meta}, [], source, [source], now=100)
                row = store.memories.snapshots([identifier])[identifier]
                for presentation in ('context',):
                    evidence = format_memory(row)
                    response = await complete(
                        MEMORY_TOOL_POLICY + '\n这是虚构的演示环境，任务条件已满足。请选择下一步工具；不执行任何真实操作。',
                        [{'role': 'user', 'content': f'{evidence}\n{task}'}], TOOLS)
                    actions = [call.name for call in response.tool_calls]
                    expected = 'confirm_method' if status == 'unverified' else 'execute_method'
                    results.append({'case': key, 'verification': status, 'presentation': presentation,
                                    'actions': actions, 'expected': expected, 'passed': actions == [expected]})
            finally:
                store.close()
    return {'results': results, 'passed': all(row['passed'] for row in results)}


async def scripted_complete(system, messages, tools):
    name = 'confirm_method' if 'unverified' in str(messages) else 'execute_method'
    call = ToolCall('next', name, {})
    return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': name, 'input': {}}], [call])


async def run(live):
    output = Path(tempfile.mkdtemp(prefix='momoi-eval-practice-', dir=TEMP_ROOT))
    if live:
        async with OpenAIProvider(model_config('MOMOI_EVAL')) as provider:
            result = await evaluate_practice(provider.complete, output)
    else:
        result = await evaluate_practice(scripted_complete, output)
    result['mode'] = 'live' if live else 'scripted_smoke_only'
    write_json(output / 'result.json', result)
    print(json.dumps({'passed': result['passed'], 'mode': result['mode'], 'output': str(output)}))
    return int(not result['passed'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use explicitly configured MOMOI_EVAL model')
    raise SystemExit(asyncio.run(run(parser.parse_args().live)))
