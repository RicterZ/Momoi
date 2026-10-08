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
