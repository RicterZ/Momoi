"""Bounded historical execution evidence from native and legacy journals."""
import json
from ...tools.presentation import fit_result, present_result
from itertools import groupby

EXCLUDED = {'recall', 'end_turn', 'heartbeat_end_turn', 'reply', 'send_bubbles', 'send_voice'}


def clip(text, limit=100, terms=()):
    text = str(text)
    if len(text) <= limit:
        return text
    marker = '[...truncated...]'
    hits = [text.casefold().find(term) for term in terms if term and term in text.casefold()]
    if hits:
        width = max(1, limit - 2 * len(marker))
        start = max(0, min(hits) - width // 3)
        end = min(len(text), start + width)
        return (marker if start else '') + text[start:end] + (marker if end < len(text) else '')
    edge = max(1, (limit - len('[...truncated...]')) // 2)
    return text[:edge] + '[...truncated...]' + text[-edge:]


def bounded(value, limit=160, depth=0, terms=()):
    if isinstance(value, str):
        return clip(value, limit, terms)
    if isinstance(value, dict):
        if depth >= 3:
            return clip(json.dumps(value, ensure_ascii=False), limit)
        return {k: bounded(v, limit, depth + 1, terms) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        return [bounded(v, limit, depth + 1, terms) for v in value[:3]]
    return value


def eligible(exchange):
    content = exchange.get('content')
    if not isinstance(content, list):
        return None
    all_calls = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_use']
    calls = [b for b in all_calls if b.get('name') not in EXCLUDED]
    text = '\n'.join(str(b.get('text', '')) for b in content
                     if isinstance(b, dict) and b.get('type') == 'text')
    if not calls and all_calls:
        return None
    if not calls and not text:
        return None
    results = {b.get('tool_use_id'): b.get('content', '') for b in exchange.get('results', [])
               if isinstance(b, dict) and b.get('type') == 'tool_result'}
    tools = []
    for call in calls:
        raw = results.get(call.get('id'))
        try:
            result = json.loads(raw) if raw is not None else {'error': 'result_not_recorded', 'ambiguous': True}
        except (TypeError, ValueError):
            result = {'content': raw}
        if isinstance(result, dict):
            result = {key: value for key, value in result.items() if key != 'provenance'}
        tools.append({'call_id': call.get('id'), 'name': call.get('name'),
                      'arguments': call.get('input', {}),
                      'arguments_complete': call.get('_arguments_complete', True), 'result': result})
    return {**({'assistant_text': text} if text else {}), **({'tools': tools} if tools else {})}


def bounded_result(value, terms=(), budget=800):
    """Bound the complete presentation while preserving outcome and read-back data."""
    if isinstance(value, dict) and isinstance(value.get("result"), (dict, list)):
        return fit_result(value, budget, string_limit=160)
    reduced = bounded(value, terms=terms)
    encode = lambda item: json.dumps(item, ensure_ascii=False, separators=(',', ':'))
    if isinstance(value, dict) and value.get('result_ref'):
        if not isinstance(reduced, dict):
            reduced = {'content': reduced}
        reduced['result_ref'] = value['result_ref']
    if len(encode(reduced)) <= budget:
        return reduced
    essential = ('ok', 'state', 'error', 'exit_code', 'ambiguous', 'image_id', 'result_ref')
    result = {key: reduced[key] for key in essential if isinstance(reduced, dict) and key in reduced}
    result['truncated'] = True
    body = encode({k: v for k, v in value.items() if k not in essential and k != 'truncated'}) if isinstance(value, dict) else encode(value)
    # Account for JSON escaping as well as the excerpt itself.
    low, high = 40, budget
    best = ''
    while low <= high:
        width = (low + high) // 2
        excerpt = clip(body, width, terms)
        if len(encode({**result, 'content': excerpt})) <= budget:
            best = excerpt
            low = width + 1
        else:
            high = width - 1
    result['content'] = best
    return result


def historical_result(value, tool_name=None):
    """Remove transport metadata only from the historical presentation."""
    if not isinstance(value, dict):
        return value
    value = present_result(value, historical=True, tool_name=tool_name)
    if value.get('result_ref') and 'chunk_start' in value and 'content' in value:
        for key in ('format', 'sha256', 'original_chars', 'chunk_start', 'chunk_end',
                    'next_cursor', 'has_more'):
            value.pop(key, None)
    return value


def journal_rows(db, episode_id=None, after=None, before=None, before_ordinal=None):
    rows = db.execute('''SELECT et.episode_id, et.turn_id, et.ordinal, t.started_at,
        j.sequence, j.created_at AS journal_created_at, j.payload_json, j.item_type FROM episode_turns et JOIN turns t ON t.id=et.turn_id
        LEFT JOIN turn_journal j ON j.turn_id=et.turn_id AND (
            j.item_type='assistant_exchange' OR (
                j.item_type IN ('tool_call', 'tool_result') AND NOT EXISTS (
                    SELECT 1 FROM turn_journal native WHERE native.turn_id=et.turn_id
                    AND native.item_type='assistant_exchange')))
        WHERE (? IS NULL OR et.episode_id=?) AND (? IS NULL OR et.ordinal<?)
        AND (? IS NULL OR t.started_at>=?) AND (? IS NULL OR t.started_at<?)
        ORDER BY et.episode_id, et.ordinal, j.sequence''',
        (episode_id, episode_id, before_ordinal, before_ordinal, after, after, before, before)).fetchall()
    output = []
    for _, records in groupby(rows, key=lambda row: (row['episode_id'], row['turn_id'])):
        records = list(records)
        if records[0]['item_type'] in (None, 'assistant_exchange'):
            output.extend(records)
            continue
        # Pair by ID, never adjacency: concurrent calls may finish out of order.
        results = {}
        for row in records:
            if row['item_type'] == 'tool_result':
                payload = json.loads(row['payload_json'])
                if payload.get('tool_call_id'):
                    results[payload['tool_call_id']] = payload
        for row in records:
            if row['item_type'] != 'tool_call':
                continue
            call = json.loads(row['payload_json'])
            identifier = call.get('tool_call_id')
            result = results.get(identifier)
            exchange = {'content': [{'type': 'tool_use', 'id': identifier,
                         'name': call.get('name'), 'input': call.get('arguments', {}),
                         '_arguments_complete': call.get('arguments_complete', False)}],
                        'results': []}
            if result is not None:
                value = result.get('result')
                if not isinstance(value, dict):
                    value = {'content': value}
                value = {**value}
                for key in ('ok', 'error'):
                    if key in result:
                        value.setdefault(key, result[key])
                exchange['results'].append({'type': 'tool_result', 'tool_use_id': identifier,
                                            'content': json.dumps(value, ensure_ascii=False)})
            # Keep the original call sequence for stable deep-read cursors.
            output.append({**dict(row), 'payload_json': json.dumps(exchange, ensure_ascii=False)})
    return output


def search_fields(rows):
    grouped = {}
    for row in rows:
        if not row['payload_json']:
            continue
        entry = eligible(json.loads(row['payload_json']))
        if entry:
            grouped.setdefault(row['episode_id'], []).append(json.dumps(entry, ensure_ascii=False))
    return {key: '\n'.join(value) for key, value in grouped.items()}


def execution_turns(store, episode_id, keywords=(), *, limit=3, tool_limit=3,
                    after=None, before=None, before_ordinal=None, selected_messages=(),
                    turn_id=None, after_sequence=0, after_turn_ordinal=None):
    rows = journal_rows(store._db, episode_id, after, before, before_ordinal)
    row_by_sequence = {(row['turn_id'], row['sequence']): row for row in rows}
    turns = {}
    terms = [str(t).casefold() for t in keywords if t]
    message_turns = {str(m['turn_id']) for m in selected_messages}
    for row in rows:
        if after_turn_ordinal is not None and row['ordinal'] <= after_turn_ordinal:
            continue
        if turn_id and row['turn_id'] != turn_id:
            continue
        if row['sequence'] is not None and row['sequence'] <= after_sequence:
            continue
        turn = turns.setdefault(row['turn_id'], {'id': row['turn_id'],
            'time': store.context_timestamp(row['started_at']), 'ordinal': row['ordinal'],
            'execution': [], 'score': 0, 'journal_missing': True})
        if row['payload_json']:
            turn['journal_missing'] = False
            entry = eligible(json.loads(row['payload_json']))
            if entry:
                text = json.dumps(entry, ensure_ascii=False).casefold()
                score = sum(term in text for term in terms)
                turn['execution'].append((score, row['sequence'], entry))
                turn['score'] += score
    candidates = [t for t in turns.values() if not terms or t['score'] or t['id'] in message_turns]
    # Summary-only / semantic-only hits still offer recent execution context.
    if not candidates:
        candidates = list(turns.values())
    if after_turn_ordinal is not None:
        candidates.sort(key=lambda t: t['ordinal'])
    else:
        candidates.sort(key=lambda t: (t['id'] in message_turns, t['score'], t['ordinal']), reverse=True)
    chosen = sorted(candidates[:limit], key=lambda t: t['ordinal'])
    timings = {}
    if chosen and (turn_id is not None or after_turn_ordinal is not None):
        identifiers = [turn['id'] for turn in chosen]
        marks = ','.join('?' for _ in identifiers)
        for row in store._db.execute(
            f"""SELECT turn_id,sequence,created_at,item_type,payload_json
                FROM turn_journal WHERE turn_id IN ({marks})
                AND item_type IN ('tool_call','tool_result') ORDER BY turn_id,sequence""",
            identifiers,
        ):
            payload = json.loads(row['payload_json'])
            identifier = payload.get('tool_call_id')
            if not identifier:
                continue
            timing = timings.setdefault((row['turn_id'], identifier), {})
            prefix = 'call' if row['item_type'] == 'tool_call' else 'result'
            timing[prefix + '_sequence'] = row['sequence']
            timing['called_at' if prefix == 'call' else 'finished_at'] = row['created_at']
            timing['called_time' if prefix == 'call' else 'finished_time'] = store.context_timestamp(row['created_at'])
    output = []
    for turn in chosen:
        entries = sorted(turn['execution'], key=lambda item: (0 if turn_id else -item[0], item[1]))
        remaining = tool_limit
        selected = []
        total = sum(len(entry.get('tools', [])) for _, _, entry in entries)
        for _, sequence, entry in entries:
            if remaining <= 0:
                break
            value = {}
            if turn_id is not None or after_turn_ordinal is not None:
                source = row_by_sequence[(turn['id'], sequence)]
                value.update(sequence=sequence, recorded_at=source['journal_created_at'],
                             recorded_time=store.context_timestamp(source['journal_created_at']))
            if entry.get('assistant_text'):
                value['assistant_text'] = (entry['assistant_text']
                                           if turn_id is not None or after_turn_ordinal is not None
                                           else clip(entry['assistant_text'], terms=terms))
            calls = []
            batch = entry.get("tools", [])
            if (turn_id is not None or after_turn_ordinal is not None) and selected and len(batch) > remaining:
                break
            if turn_id is None and after_turn_ordinal is None:
                ranked = sorted(enumerate(batch), key=lambda pair: (
                    -sum(term in json.dumps(pair[1], ensure_ascii=False).casefold() for term in terms), pair[0]))
                batch = [call for _, call in sorted(ranked[:remaining])]
            for call in batch:
                result = historical_result(call['result'], call['name'])
                reduced = {'name': call['name'], 'arguments': bounded(call['arguments'], terms=terms),
                           'result': bounded_result(result, terms=terms)}
                if reduced['arguments'] != call['arguments']:
                    reduced['arguments_truncated'] = True
                    reduced['arguments_read'] = {'episode_id': episode_id, 'turn_id': turn['id'],
                                                 'tool_call_id': call['call_id'],
                                                 'after_sequence': sequence - 1}
                if timing := timings.get((turn['id'], call['call_id'])):
                    reduced['timing'] = timing
                if not call['arguments_complete']:
                    reduced['arguments_complete'] = False
                if reduced['result'] != result:
                    if not isinstance(reduced['result'], dict):
                        reduced['result'] = {'content': reduced['result']}
                    reduced['result']['truncated'] = True
                # References must survive metadata trimming.
                if isinstance(call['result'], dict) and call['result'].get('result_ref'):
                    reduced['result']['result_ref'] = call['result']['result_ref']
                calls.append(reduced)
            if calls:
                value['tools'] = calls
            remaining -= max(1, len(calls))
            selected.append((sequence, value))
        item = {'id': turn['id'], 'time': turn['time']}
        if after_turn_ordinal is not None:
            item['ordinal'] = turn['ordinal']
        msgs = [{'role': 'owner' if m['role'] == 'user' else m['role'], 'text': clip(m['content']),
                 **({'quote_targets': targets} if (targets := store.message_quote_targets(m.get('id'))) else {})}
                for m in selected_messages if str(m['turn_id']) == turn['id']][:3]
        if turn_id is not None or after_turn_ordinal is not None:
            # Execution reads include the dialogue of the selected Turn. Keep
            # actual timestamps so speech between two tool batches stays there.
            message_rows = store._db.execute(
                'SELECT id FROM messages WHERE turn_id=? ORDER BY created_at,id',
                (turn['id'],),
            ).fetchall()
            msgs = [store.conversation_message(episode_id, row['id']) for row in message_rows]
        if msgs:
            item['messages'] = msgs
        if selected:
            item['execution'] = [v for _, v in sorted(selected)]
        shown = sum(len(v.get('tools', [])) for _, v in selected)
        if total > shown:
            item['omitted_tool_calls'] = total - shown
        if (turn_id is not None or after_turn_ordinal is not None) and selected and len(selected) < len(entries):
            item['next_after_sequence'] = max(seq for seq, _ in selected)
        if turn['journal_missing']:
            item['journal_available'] = False
        output.append(item)
    result = {'turns': output}
    if len(candidates) > len(chosen):
        result['omitted_turns'] = len(candidates) - len(chosen)
        # Default evidence is ranked; restart chronological browsing to cover
        # every Turn, including those with no archived chat messages.
        result['next_execution_cursor'] = chosen[-1]['ordinal'] if after_turn_ordinal is not None else 0
    return result


def execution_arguments(store, episode_id, turn_id, tool_call_id, after_sequence=0):
    """Read original inputs by journal location, without re-executing the tool."""
    for row in journal_rows(store._db, episode_id):
        if row['turn_id'] != turn_id or row['sequence'] is None or row['sequence'] <= after_sequence:
            continue
        entry = eligible(json.loads(row['payload_json']))
        for call in entry.get('tools', []) if entry else []:
            if call['call_id'] == tool_call_id:
                if not call['arguments_complete']:
                    return {'ok': False, 'error': 'complete_arguments_unavailable',
                            'message': 'Legacy journal saved only a log projection; original arguments cannot be recovered.',
                            'stored_arguments': call['arguments']}
                return {'ok': True, 'tool': call['name'], 'arguments': call['arguments']}
    return {'ok': False, 'error': 'tool_call_not_found'}
