"""Frozen recall excerpts, changed only at a shared compaction boundary."""
import json

EDGE = 80


def excerpt(text):
    if not isinstance(text, str) or len(text) <= EDGE * 2:
        return text
    return text[:EDGE] + '\n[...truncated...]\n' + text[-EDGE:]


def compact_recall(payload):
    if not isinstance(payload, dict) or payload.get('ok') is False:
        return payload
    result = {key: payload[key] for key in ('ok', 'error', 'state', 'result_ref', 'memory', 'reflection', 'reflection_note') if key in payload}
    for kind in ('memory', 'reflection'):
        if isinstance(result.get(kind), list):
            result[kind] = [
                {key: excerpt(value) for key, value in item.items()
                 if key in {'id', 'kind', 'key', 'content', 'local_date', 'confidence', 'evidence'}}
                if isinstance(item, dict) else excerpt(item) for item in result[kind]
            ]
        elif kind in result:
            result[kind] = excerpt(result[kind])
    if isinstance(payload.get('episodes'), list):
        result['episodes'] = [{
            **{key: excerpt(item[key]) for key in ('id', 'title', 'summary') if key in item},
            'details_omitted': True,
        } for item in payload['episodes'] if isinstance(item, dict)]
    result.update(history_truncated=True, details_omitted=True,
                  evidence_note='历史召回已剪裁；省略的原文不构成已确认依据。需要细节时用 result_ref 读取完整结果、episode_read 读取话题，或重新检索。')
    return result


def compact_recall_messages(messages, turn_ids):
    """Apply the persisted boundary to provider-neutral replay messages in place."""
    selected = set(turn_ids)
    ids = {block.get('id') for message in messages if selected.intersection(message.get('_history_turn_ids', ()))
           for block in (message.get('content') if isinstance(message.get('content'), list) else [])
           if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'recall'}
    for message in messages:
        if not selected.intersection(message.get('_history_turn_ids', ())):
            continue
        for block in message.get('content', []) if isinstance(message.get('content'), list) else []:
            if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id') in ids:
                try:
                    payload = json.loads(block['content'])
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and not payload.get('history_truncated'):
                    block['content'] = json.dumps(compact_recall(payload), ensure_ascii=False)
