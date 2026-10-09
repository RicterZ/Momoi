"""Private weekly synthesis, deliberately separate from memory promotion."""
import asyncio
import json
import logging

from ...observability.events import log_event
from xml.sax.saxutils import escape, quoteattr

from ..agent import AgentWorkflow
from ..turn_support import PROMPT_ROOT, live_prompt

logger = logging.getLogger("momoi.runtime.turns")

PROMPT_PATH = PROMPT_ROOT.joinpath('weekly_reflection.md')
PROMPT = PROMPT_PATH.read_text(encoding='utf-8').strip()

WEEKLY_REFLECTION_FINISH_SPEC = {
    'name': 'weekly_reflection_finish',
    'description': '保存本周盘点结果并结束本轮；不写入长期记忆或召回库。',
    'input_schema': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'summary': {'type': 'string', 'minLength': 1, 'maxLength': 4000,
                        'description': '用一两句话说明材料覆盖和主要局限，不复述候选列表。'},
            'findings': {
                'type': 'array', 'maxItems': 16,
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'properties': {
                        'key': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                                'description': '不含日期的稳定主题键。'},
                        'assessment': {'type': 'string', 'enum': ['explicit', 'recurring', 'change', 'unresolved'],
                                       'description': 'explicit：用户明确表达的事实、状态或要求，单日即可；recurring：跨日独立经历支持的模式；change：有依据的变化；unresolved：尚待确认的认识。不要求每类都有。'},
                        'content': {'type': 'string', 'minLength': 1, 'maxLength': 1000,
                                    'description': '通常一句话，直接陈述值得记住的认识及必要适用范围；不写日期标签、事件经过或论证。'},
                        'uncertainty': {'type': 'string', 'maxLength': 1000,
                                        'description': '证据缺口与适用边界；无需时为空。'},
                    },
                    'required': ['key', 'assessment', 'content', 'uncertainty'],
                },
            },
        },
        'required': ['summary', 'findings'],
    },
}


def weekly_reflection_input(source):
    lines = [f'<weekly_observations start={quoteattr(source["period_start"])} '
             f'end_exclusive={quoteattr(source["period_end"])}>']
    for day in source['days']:
        lines.append(f'<day date={quoteattr(day["date"])} state={quoteattr(day["state"])}>')
        for item in day['observations']:
            lines.append(f'<observation id={quoteattr(item["id"])} kind={quoteattr(item["kind"])} '
                         f'key={quoteattr(item["key"])}>')
            for key in ('content', 'evidence', 'confidence'):
                lines.append(f'<{key}>{escape(str(item[key]))}</{key}>')
            lines.append('</observation>')
        lines.append('</day>')
    lines.append('</weekly_observations>')
    return '\n'.join(lines)


def parse_weekly_reflection(arguments):
    from ...tools.validation import validate_tool_arguments
    value, error = validate_tool_arguments(
        'weekly_reflection_finish', arguments, WEEKLY_REFLECTION_FINISH_SPEC['input_schema'],
    )
    if error:
        return None, error
    keys = set()
    for item in value['findings']:
        if not item['content'].strip() or not item['key'].strip() or item['key'] in keys:
            return None, {'ok': False, 'error': 'invalid_or_duplicate_finding'}
        keys.add(item['key'])
    if not value['summary'].strip():
        return None, {'ok': False, 'error': 'empty_summary'}
    return value, None


class WeeklyReflectionWorkflow:
    async def _complete_weekly_reflection_turn(self, period_end, stop):
        record = self.store.weekly_reflection(period_end)
        if record is None or record['state'] != 'running':
            return
        turn_id = self._turn_id('weekly-reflection', period_end, record['claimed_at'])
        state = self.store.begin_turn(turn_id, 'weekly_reflection', [f'weekly-reflection:{period_end}'])
        if state != 'running' or stop.is_set():
            self.store.release_weekly_reflection(period_end, 'turn_not_ready')
            return
        source = json.loads(record['input_json'])
        completed = False

        async def execute(call):
            nonlocal completed
            result, error = parse_weekly_reflection(call.arguments)
            if error:
                return error
            self.store.commit_weekly_reflection(period_end, turn_id, result)
            completed = True
            return {'ok': True, 'state': 'completed', 'findings': len(result['findings'])}

        try:
            if not any(day['observations'] for day in source['days']):
                self.store.commit_weekly_reflection(period_end, turn_id, {
                    'summary': '本期没有可供盘点的每日观察，不作归纳。', 'findings': [],
                })
                return
            workflow = AgentWorkflow(
                stage='weekly_reflection', preserve_transcript=True,
                tool_names=frozenset({'weekly_reflection_finish'}), execute_tool=execute,
                is_complete=lambda: completed, completion_result=lambda: {'ok': completed},
                no_tool_correction='Call weekly_reflection_finish to submit the weekly review.',
            )
            await self._run_agent_workflow(
                live_prompt(PROMPT_PATH, PROMPT),
                [{'role': 'user', 'content': weekly_reflection_input(source)}],
                [WEEKLY_REFLECTION_FINISH_SPEC], turn_id=turn_id, workflow=workflow,
            )
            if not completed:
                raise RuntimeError('weekly_reflection_incomplete')
        except asyncio.CancelledError:
            self.store.record_turn_failure(turn_id, 'weekly_reflection_interrupted')
            self.store.release_weekly_reflection(period_end, 'interrupted')
            raise
        except Exception as error:
            log_event(logger, logging.ERROR, 'weekly_reflection_failed',
                      stage='weekly_reflection', period_end=period_end, error_type=type(error).__name__, exc_info=True)
            self.store.record_turn_failure(turn_id, type(error).__name__)
            self.store.release_weekly_reflection(period_end, type(error).__name__)
        finally:
            self.agenda_changed.set()
