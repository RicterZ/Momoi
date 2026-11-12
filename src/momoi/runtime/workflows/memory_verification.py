"""Weekly check of confirmed memories against later conversations; never edits memories."""
import asyncio
import json
import logging

from ...observability.events import log_event
from ...storage.memory.verification import VERDICTS
from xml.sax.saxutils import escape

from ..agent import AgentWorkflow
from ..turn_support import PROMPT_ROOT, live_prompt

logger = logging.getLogger("momoi.runtime.turns")

PROMPT_PATH = PROMPT_ROOT.joinpath('memory_verification.md')
PROMPT = PROMPT_PATH.read_text(encoding='utf-8').strip()

MEMORY_VERIFICATION_FINISH_SPEC = {
    'name': 'memory_verification_finish',
    'description': '提交每条记忆的核对结论并结束本轮；结论只进入待审列表，不修改记忆。',
    'input_schema': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'items': {
                'type': 'array', 'maxItems': 50,
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'properties': {
                        'memory_id': {'type': 'integer'},
                        'verdict': {'type': 'string', 'enum': list(VERDICTS)},
                        'conflicts_with': {'type': 'integer',
                                           'description': '仅 verdict 为 conflicts_with 时填写 related_memories 中的记忆 ID。'},
                        'citations': {'type': 'array', 'maxItems': 4, 'items': {
                            'type': 'object', 'additionalProperties': False,
                            'properties': {
                                'source': {'type': 'string', 'minLength': 1,
                                           'description': '材料中的 evidence:N、message:N 或 memory:N。'},
                                'quote': {'type': 'string', 'minLength': 1, 'maxLength': 400,
                                          'description': '从该来源逐字复制的连续原文。'},
                            },
                            'required': ['source', 'quote'],
                        }},
                        'reason': {'type': 'string', 'minLength': 1, 'maxLength': 400},
                    },
                    'required': ['memory_id', 'verdict', 'citations', 'reason'],
                },
            },
        },
        'required': ['items'],
    },
}


def memory_verification_input(source):
    lines = ['<memory_verification>']
    for item in source['items']:
        lines.append('<item>' + escape(json.dumps(item, ensure_ascii=False)) + '</item>')
    lines.append('</memory_verification>')
    return '\n'.join(lines)


def parse_memory_verification(arguments):
    from ...tools.validation import validate_tool_arguments
    value, error = validate_tool_arguments(
        'memory_verification_finish', arguments, MEMORY_VERIFICATION_FINISH_SPEC['input_schema'],
    )
    if error:
        return None, error
    for item in value['items']:
        if not item['reason'].strip() or any(not row['quote'].strip() for row in item['citations']):
            return None, {'ok': False, 'error': 'empty_reason_or_quote'}
    return value, None


class MemoryVerificationWorkflow:
    async def _complete_memory_verification_turn(self, period_end, stop):
        record = self.store.memory_verification_run(period_end)
        if record is None or record['state'] != 'running':
            return
        turn_id = self._turn_id('memory-verification', period_end, record['claimed_at'])
        state = self.store.begin_turn(turn_id, 'memory_verification', [f'memory-verification:{period_end}'])
        if state != 'running' or stop.is_set():
            self.store.release_memory_verification(period_end, 'turn_not_ready')
            return
        source = json.loads(record['input_json'])
        completed = False

        async def execute(call):
            nonlocal completed
            result, error = parse_memory_verification(call.arguments)
            if error:
                return error
            try:
                self.store.commit_memory_verification(period_end, turn_id, result)
            except ValueError as error:
                return {'ok': False, 'error': str(error)}
            completed = True
            return {'ok': True, 'state': 'completed', 'items': len(result['items'])}

        try:
            if not source['items']:
                self.store.commit_memory_verification(period_end, turn_id, {'items': []})
                return
            workflow = AgentWorkflow(
                stage='memory_verification', preserve_transcript=True,
                tool_names=frozenset({'memory_verification_finish'}), execute_tool=execute,
                is_complete=lambda: completed, completion_result=lambda: {'ok': completed},
                no_tool_correction='Call memory_verification_finish to submit the verification.',
            )
            await self._run_agent_workflow(
                live_prompt(PROMPT_PATH, PROMPT),
                [{'role': 'user', 'content': memory_verification_input(source)}],
                [MEMORY_VERIFICATION_FINISH_SPEC], turn_id=turn_id, workflow=workflow,
            )
            if not completed:
                raise RuntimeError('memory_verification_incomplete')
        except asyncio.CancelledError:
            self.store.record_turn_failure(turn_id, 'memory_verification_interrupted')
            self.store.release_memory_verification(period_end, 'interrupted')
            raise
        except Exception as error:
            log_event(logger, logging.ERROR, 'memory_verification_failed',
                      stage='memory_verification', period_end=period_end,
                      error_type=type(error).__name__, exc_info=True)
            self.store.record_turn_failure(turn_id, type(error).__name__)
            self.store.release_memory_verification(period_end, type(error).__name__)
        finally:
            self.agenda_changed.set()
