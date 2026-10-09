"""Model-facing projections; never mutate durable tool payloads."""

import json


def mcp_body(value):
    """Decode only recognized MCP text/structured wrappers, without guessing."""
    for _ in range(12):
        if isinstance(value, dict):
            if isinstance(value.get('structuredContent'), (dict, list)):
                value = value['structuredContent']
            elif isinstance(value.get('content'), list) and value['content'] and all(
                isinstance(item, dict) and item.get('type') == 'text'
                and isinstance(item.get('text'), str) for item in value['content']
            ) and set(value) <= {'content', 'isError', '_meta'}:
                value = value['content']
            else:
                return value
        elif isinstance(value, list) and value and all(
            isinstance(item, dict) and item.get('type') == 'text'
            and isinstance(item.get('text'), str) for item in value
        ):
            # Separate text blocks need not form a single JSON document.
            parts = [mcp_body(item['text']) for item in value]
            return parts[0] if len(parts) == 1 else parts
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                return value
            if not isinstance(decoded, (dict, list)):
                return value
            value = decoded
        else:
            return value
    return value


def present_result(value, *, historical=False, tool_name=None, display_only=False):
    if not isinstance(value, dict):
        return value
    provenance = value.get('provenance') or {}
    tool_name = tool_name or provenance.get('tool', '')
    result = {key: item for key, item in value.items() if key != 'provenance'}
    if result.get('ok') is True or tool_name == 'web_fetch':
        result = project_tool_result(result, tool_name)
    if isinstance(result.get('result'), dict) and (
        'content' in result['result'] or 'structuredContent' in result['result']
    ):
        result['result'] = mcp_body(result['result'])
    if result.get('error') is None:
        result.pop('error', None)
    if result.get('truncated') is False:
        result.pop('truncated', None)
    if historical:
        result.pop('sha256', None)
        if result.get('stderr_tail') == '':
            result.pop('stderr_tail')
    if (historical or display_only) and tool_name == 'reply' and result.get('ok') is True:
        result.pop('ok', None)
        if result.get('state') == 'committed':
            result.pop('state')
    return result


# Keep outcomes and continuation values before descriptive bodies at every level.
_OUTCOME_KEYS = ('ok', 'error', 'message', 'state', 'status', 'delivery_state',
                 'exit_code', 'ambiguous', 'id', 'plan_id', 'operation_id',
                 'path', 'source', 'destination', 'version', 'next_cursor',
                 'has_more', 'next_content_offset', 'result_ref', 'result', 'episode', 'messages',
                 'ordinal', 'turn_id', 'message_id', 'content_offset',
                 'next_before_ordinal', 'next_execution_cursor', 'next_after_sequence')


def fit_result(value, budget, *, string_limit=1000):
    """Keep valid business structure and report omitted paths, never JSON fragments."""
    encode = lambda item: json.dumps(item, ensure_ascii=False, default=str)
    if len(encode(value)) <= budget:
        return value
    from .episode_timeline import fit_dialogue_result, fit_execution_result
    execution = fit_execution_result(value, budget)
    if execution is not None:
        return execution
    dialogue = fit_dialogue_result(value, budget)
    if dialogue is not None:
        return dialogue
    for width, count in [(string_limit, 10), (240, 5), (120, 3), (60, 2), (24, 1)]:
        omitted = []

        def reduce(item, path='', depth=0):
            if isinstance(item, str):
                if len(item) <= width or path == 'result_ref':
                    return item
                omitted.append(path)
                return item[:width // 2] + '[...truncated...]' + item[-width // 2:]
            if isinstance(item, dict):
                keys = [k for k in _OUTCOME_KEYS if k in item]
                rest = [k for k in item if k not in keys]
                rest.sort(key=lambda k: isinstance(item[k], (dict, list)) or
                          isinstance(item[k], str) and len(item[k]) > 160)
                keys += rest
                selected = keys[:max(count, len([k for k in keys if k in _OUTCOME_KEYS]))]
                omitted.extend(f'{path}.{k}'.lstrip('.') for k in keys if k not in selected)
                if depth >= 12:
                    omitted.append(path)
                    return {}
                if isinstance(item.get('content'), str) and 'next_content_offset' in item and 'content' not in selected:
                    selected.append('content')
                reduced = {k: reduce(item[k], f'{path}.{k}'.lstrip('.'), depth + 1) for k in selected}
                if isinstance(item.get('content'), str) and 'next_content_offset' in item and len(item['content']) > width:
                    reduced['content'] = item['content'][:width]
                    reduced['content_offset'] = item.get('content_offset', 0)
                    reduced['next_content_offset'] = reduced['content_offset'] + width
                messages = reduced.get('messages')
                if isinstance(messages, list) and len(messages) < len(item.get('messages', [])):
                    ordinals = [m['ordinal'] for m in messages if isinstance(m, dict) and 'ordinal' in m]
                    if ordinals:
                        reduced['next_before_ordinal'] = min(ordinals)
                        reduced['truncated'] = True
                return reduced
            if isinstance(item, list):
                if depth >= 12:
                    omitted.append(path)
                    return []
                selected = list(enumerate(item[:count]))
                if path.endswith('messages') and all(isinstance(v, dict) and 'ordinal' in v for v in item):
                    ordinals = sorted({v['ordinal'] for v in item})[-count:]
                    selected = [(i, v) for i, v in enumerate(item) if v['ordinal'] in ordinals]
                if len(selected) < len(item):
                    omitted.append(f'{path} (entries outside excerpt)')
                return [reduce(v, f'{path}[{i}]', depth + 1) for i, v in selected]
            return item

        result = reduce(value)
        if not isinstance(result, dict):
            result = {'content': result}
        result.update(truncated=True, omitted_fields=omitted[:8])
        if len(omitted) > 8:
            result['additional_omitted_fields'] = len(omitted) - 8
        if len(encode(result)) <= budget:
            return result
    # Extremely wide objects still retain outcome fields and a deep-read pointer.
    result = {k: result[k] for k in ('ok', 'error', 'state', 'status', 'exit_code', 'ambiguous', 'result_ref') if k in result}
    result.update(truncated=True, omitted_fields=['business_body'])
    return result


def project_tool_result(result, name):
    """Tool-specific semantics are projected after saving the complete snapshot."""
    if name == 'skill_load' and isinstance(result.get('content'), str):
        omitted = [key for key in ('name', 'description', 'resources') if key in result]
        for key in omitted:
            result.pop(key)
        if omitted:
            result['omitted_fields'] = sorted(set(result.get('omitted_fields', []) + omitted))
    if name == 'web_fetch':
        omitted = []
        if result.get('requested_url') == result.get('url'):
            omitted.append('requested_url')
        if result.get('ok') is True and result.get('status') == 200:
            omitted.append('status')
        if result.get('ok') is True and result.get('content_type') in {'text/html', 'text/plain', 'application/xhtml+xml'}:
            omitted.append('content_type')
        if not result.get('source_truncated'):
            omitted.append('source_truncated')
        if not result.get('truncated'):
            omitted.append('content_length')
        if not result.get('title'):
            omitted.append('title')
        omitted.append('extract_mode')
        for key in omitted:
            result.pop(key, None)
    if name == 'episode_relations' and isinstance(result.get('edges'), list) and isinstance(result.get('nodes'), list):
        if 'cursor' in result or 'limit' in result or 'next_cursor' not in result:
            cursor = result.pop('cursor', 0)
            limit = result.pop('limit', 20)
            edges = result['edges']
            result['edges'] = edges[cursor:cursor + limit]
            ids = {result['root_episode_id']}
            for edge in result['edges']:
                ids.update((edge['source_episode_id'], edge['target_episode_id']))
            nodes = []
            for item in result['nodes']:
                if item['id'] in ids:
                    node = dict(item)
                    summary = str(node.get('summary') or '')
                    node['summary'] = summary[:240]
                    nodes.append(node)
            result.update(nodes=nodes, total_relations=len(edges),
                          next_cursor=cursor + limit if cursor + limit < len(edges) else None)
            result['omitted_fields'] = ['nodes.*.summary (excerpt)', 'edges/nodes (outside page)']
    if name == 'read_file' and isinstance(result.get('lines'), list):
        lines = result.pop('lines')
        result['content'] = ''.join(item['text'] for item in lines)
        if lines:
            result['start_line'] = lines[0]['line']
            result['end_line'] = lines[-1]['line']
    if name in {'goal_create', 'goal_update', 'goal_finish', 'goal_cancel', 'goal_review'} and isinstance(result.get('goal'), dict):
        goal = result.pop('goal')
        result.update(goal_id=goal['id'], status=goal['status'])
        for key in ('title', 'next_review_at', 'schedule'):
            if goal.get(key) is not None:
                result[key] = goal[key]
        result['omitted_fields'] = ['goal (full staged object)']
    if name in {'plan_create', 'plan_submit', 'plan_start', 'plan_update', 'plan_cancel', 'plan_resume'}:
        fields = {'ok', 'state', 'plan_id', 'title', 'status', 'version', 'step_index', 'resume_safety',
                  'requires_owner_decision', 'result_ref', 'truncated', 'omitted_fields'}
        omitted = [key for key in result if key not in fields]
        result = {key: value for key, value in result.items() if key in fields}
        if omitted:
            result['omitted_fields'] = sorted(set(result.get('omitted_fields', []) + omitted))
    if name == 'episode_read':
        from .episode_timeline import project_episode
        result = project_episode(result)
    if name == 'thinking_read' and isinstance(result.get('calls'), list):
        from ..memory.text import truncate_tokens
        fields = {'turn_id', 'call_id', 'created_at', 'stage', 'round', 'tools', 'reasoning_chars', 'reasoning'}
        calls = []
        omitted = set(result.get('omitted_fields', []))
        for item in result['calls']:
            if not isinstance(item, dict):
                continue
            omitted.update(f'calls.*.{key}' for key in item if key not in fields)
            call = {key: value for key, value in item.items() if key in fields}
            reasoning = str(call.get('reasoning') or '')
            call['reasoning'] = truncate_tokens(reasoning, 1800)
            if call['reasoning'] != reasoning:
                omitted.add('calls.*.reasoning (excerpt)')
            calls.append(call)
        result['calls'] = calls
        if omitted:
            result['omitted_fields'] = sorted(omitted)
    if name == 'memory_search' and isinstance(result.get('results'), list):
        fields = {'id', 'kind', 'key', 'content', 'source', 'local_date', 'confidence',
                  'evidence', 'evidence_quote', 'updated_at', 'activation', 'authority'}
        removed = sorted({key for item in result['results'] if isinstance(item, dict)
                          for key in item if key not in fields})
        result['results'] = [{key: value for key, value in item.items() if key in fields}
                             if isinstance(item, dict) else item for item in result['results']]
        if removed:
            result['omitted_fields'] = [f'results.*.{key}' for key in removed]
    return result
