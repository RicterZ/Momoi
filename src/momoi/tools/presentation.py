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


def present_result(value, *, historical=False):
    if not isinstance(value, dict):
        return value
    result = {key: item for key, item in value.items() if key != 'provenance'}
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
    return result


# Keep outcomes and continuation values before descriptive bodies at every level.
_OUTCOME_KEYS = ('ok', 'error', 'message', 'state', 'status', 'delivery_state',
                 'exit_code', 'ambiguous', 'id', 'plan_id', 'operation_id',
                 'path', 'source', 'destination', 'version', 'next_cursor',
                 'has_more', 'next_content_offset', 'result_ref', 'result')


def fit_result(value, budget, *, string_limit=1000):
    """Keep valid business structure and report omitted paths, never JSON fragments."""
    encode = lambda item: json.dumps(item, ensure_ascii=False, default=str)
    if len(encode(value)) <= budget:
        return value
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
                return {k: reduce(item[k], f'{path}.{k}'.lstrip('.'), depth + 1) for k in selected}
            if isinstance(item, list):
                if depth >= 12:
                    omitted.append(path)
                    return []
                if len(item) > count:
                    omitted.append(f'{path}[{count}:]')
                return [reduce(v, f'{path}[{i}]', depth + 1) for i, v in enumerate(item[:count])]
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
