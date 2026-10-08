"""Compact recalled evidence when replaying previous turns."""
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


def compact_recall_messages(messages, turn_ids, *, result_store=None):
    """Clip historical turns in place; current-turn calls are outside this replay."""
    selected = set(turn_ids)
    ids = {block.get('id') for message in messages if selected.intersection(message.get('_history_turn_ids', ()))
           for block in (message.get('content') if isinstance(message.get('content'), list) else [])
           if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'recall'}
    for message in messages:
        if not selected.intersection(message.get('_history_turn_ids', ())):
            continue
        for block in (message.get('content') if isinstance(message.get('content'), list) else []):
            if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id') in ids:
                try:
                    payload = json.loads(block['content'])
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and not payload.get('history_truncated'):
                    if payload.get('ok') is not False and 'chunk_start' in payload:
                        original = (result_store.historical_payload(str(payload.get('result_ref') or ''))
                                    if result_store is not None else None)
                        if isinstance(original, dict):
                            payload = original
                        else:
                            payload = {key: payload[key] for key in ('ok', 'result_ref') if key in payload} | {
                                'preview': excerpt(str(payload.get('content') or '')),
                            }
                    compact = compact_recall(payload)
                    if 'preview' in payload:
                        compact['preview'] = payload['preview']
                    block['content'] = json.dumps(compact, ensure_ascii=False)


def remove_folded_memory_evidence(messages, overrides):
    """Drop superseded memory records only at a shared compaction boundary.

    Current always memories are already in the rebuilt prefix. Raw journals and
    snapshots remain intact; episode/reflection evidence is not memory inventory.
    """
    from xml.etree import ElementTree
    identifiers = set(overrides)
    if not identifiers:
        return

    def clean_memory(value):
        if isinstance(value, list):
            return [item for item in value if not isinstance(item, dict)
                    or str(item.get('id')) not in identifiers]
        if isinstance(value, str):
            try:
                root = ElementTree.fromstring('<records>' + value + '</records>')
            except ElementTree.ParseError:
                return value
            changed = False
            for parent in root.iter():
                for node in list(parent):
                    if node.tag == 'memory' and node.get('id') in identifiers:
                        parent.remove(node)
                        changed = True
            if changed:
                return (root.text or '') + ''.join(ElementTree.tostring(n, encoding='unicode') for n in root)
        return value

    calls = {block.get('id') for message in messages
             for block in (message.get('content') if isinstance(message.get('content'), list) else [])
             if isinstance(block, dict) and block.get('type') == 'tool_use'
             and block.get('name') == 'recall'}
    for message in messages:
        if not isinstance(message.get('content'), list):
            continue
        for block in message['content']:
            if not isinstance(block, dict) or block.get('type') != 'tool_result' or block.get('tool_use_id') not in calls:
                continue
            try:
                payload = json.loads(block['content'])
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and 'memory' in payload:
                payload['memory'] = clean_memory(payload['memory'])
                block['content'] = json.dumps(payload, ensure_ascii=False)
