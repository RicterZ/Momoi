"""Replay native assistant/tool journals without synthesizing legacy speech."""
from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from zoneinfo import ZoneInfo

from .dialogue import project_dialogue

from .results import historical_results

def render_exchanges(
    exchanges: Sequence[Mapping[str, object]],
    *, history_format: int = 4, timezone: ZoneInfo = ZoneInfo("UTC"),
    has_speech: bool = False, mark_silence: bool = True, result_store=None,
) -> list[dict[str, object]]:
    """Replay model text, tool calls, then the observations the model received."""
    exchanges = deepcopy(list(exchanges))
    project_dialogue(exchanges, timezone=timezone, has_speech=has_speech, mark_silence=mark_silence)
    historical_results(exchanges, history_format=history_format, result_store=result_store)
    messages: list[dict[str, object]] = []
    for exchange in exchanges:
        content = exchange.get("content")
        if not isinstance(content, (str, list)):
            continue
        calls = [
            block for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ] if isinstance(content, list) else []
        results = exchange.get("results")
        if not isinstance(results, list):
            continue
        call_ids = set()
        if isinstance(content, list):
            unique = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    identifier = str(block.get('id') or '')
                    if not identifier or identifier in call_ids:
                        unique.append({'type': 'text', 'text': '[invalid historical tool call] ' + json.dumps(block, ensure_ascii=False)})
                        continue
                    call_ids.add(identifier)
                unique.append(block)
            content = unique
            calls = [block for block in content if isinstance(block, dict) and block.get('type') == 'tool_use']
        paired, residue = {}, []
        for block in results:
            if not isinstance(block, dict) or block.get('type') != 'tool_result':
                residue.append(block)
                continue
            identifier = str(block.get('tool_use_id') or '')
            if identifier not in call_ids or identifier in paired:
                residue.append({'type': 'text', 'text': '[unpaired historical tool result; execution evidence only] ' + json.dumps(block, ensure_ascii=False)})
            else:
                paired[identifier] = block
        # A Turn can end before every tool in a batch runs. Keep the historical
        # API exchange valid without claiming the operation never ran.
        complete_results = []
        for call in calls:
            identifier = str(call.get("id") or "")
            if identifier in paired:
                complete_results.append(paired[identifier])
            elif identifier:
                complete_results.append({
                    "type": "tool_result", "tool_use_id": identifier,
                    "content": '{"ok":false,"error":"execution_result_unknown","ambiguous":true}',
                })
        if content:
            messages.append({"role": "assistant", "content": content})
        if complete_results or residue:
            messages.append({"role": "user", "content": [*complete_results, *residue]})
    return messages
