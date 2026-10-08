"""Text views of archived dialogue and execution; pagination stays structured."""
import json


CONTROL_TOOLS = {'end_turn', 'heartbeat_end_turn', 'recall'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def dialogue_prefix(message):
    role = {'user': 'USER', 'owner': 'USER', 'assistant': 'ASSISTANT', 'event': 'EVENT'}.get(
        message.get('role'), str(message.get('role', 'UNKNOWN')).upper())
    state = message.get('delivery_state')
    if role == 'ASSISTANT' and state and state != 'delivered':
        role += f' [{state}]'
    return f'[{message.get("timestamp", "")}] {role}: '


def message_page(messages, *, budget=6000):
    """Keep prefixes with exact continuation offsets, never alter source records."""
    lines, references = [], []
    width = max(1, budget // max(1, len(messages)))
    truncated, position = False, 0
    for raw in messages:
        message = dict(raw)
        content = str(message.get('content', message.get('text', '')))
        shown = content[:width]
        if len(content) > width:
            message['next_content_offset'] = int(message.get('content_offset', 0)) + width
            truncated = True
        metadata = {key: message[key] for key in (
            'id', 'turn_id', 'content_offset', 'next_content_offset', 'quote_targets'
        ) if key in message and message[key] is not None}
        prefix = dialogue_prefix(message)
        if metadata:
            prefix = f'[{len(references) + 1}] ' + prefix
            metadata['text_range'] = [position + len(prefix), position + len(prefix) + len(shown)]
            references.append(metadata)
        line = prefix + shown
        if len(content) > width:
            line += '\n[...truncated; continue with message_id / next_content_offset...]'
        lines.append(line)
        position += len(line) + 1
    return '\n'.join(lines), references, truncated


def fit_dialogue_result(value, budget):
    """Fit textual dialogue without advancing a cursor past omitted characters."""
    key = next((key for key in ('episode', 'message')
                if isinstance(value.get(key), dict) and 'transcript' in value[key]), None)
    if key is None:
        return None
    source = value[key]
    refs = source.get('message_refs', []) if key == 'episode' else [source]
    if not refs or any('text_range' not in ref for ref in refs):
        return None
    text = source['transcript']
    for width in (1000, 500, 240, 120, 60, 24):
        page = {**source}
        pieces, metadata, cursor, position = [], [], 0, 0
        for ref in refs:
            start, end = ref['text_range']
            before = text[cursor:start]
            content = text[start:end]
            prefix = content[:width]
            pieces.extend((before, prefix))
            position += len(before)
            updated = {key: item for key, item in ref.items() if key != 'transcript'}
            updated['text_range'] = [position, position + len(prefix)]
            if len(prefix) < len(content):
                updated['content_offset'] = ref.get('content_offset', 0)
                updated['next_content_offset'] = updated['content_offset'] + len(prefix)
            metadata.append(updated)
            position += len(prefix)
            cursor = end
        pieces.append(text[cursor:])
        page['transcript'] = ''.join(pieces)
        page['truncated'] = True
        if key == 'episode':
            page['message_refs'] = metadata
        else:
            page.update(metadata[0])
        result = {**value, key: page, 'truncated': True,
                  'omitted_fields': [f'{key}.transcript (message prefixes)']}
        if len(json.dumps(result, ensure_ascii=False, default=str)) <= budget:
            return result
    # Too much reference metadata: preserve the exact snapshot rather than
    # return a misleading message continuation cursor.
    result = {key: value[key] for key in ('ok', 'error', 'result_ref') if key in value}
    result.update(truncated=True, omitted_fields=['dialogue; read the complete result_ref'])
    return result


def fit_execution_result(value, budget):
    """Keep complete Turn timelines; never leave a cursor beyond hidden evidence."""
    turns = value.get('turns')
    if not isinstance(turns, list) or not all('timeline' in turn for turn in turns):
        return None
    encode_size = lambda item: len(json.dumps(item, ensure_ascii=False, default=str))
    for count in range(len(turns), 0, -1):
        selected = turns[:count]
        result = {**value, 'turns': selected}
        if count < len(turns):
            last_ordinal = selected[-1].get('ordinal')
            if last_ordinal is None:
                continue
            result['next_execution_cursor'] = last_ordinal
            result['omitted_turns'] = value.get('omitted_turns', 0) + len(turns) - count
            result['truncated'] = True
            result['omitted_fields'] = ['turns (whole later Turn timelines)']
        if encode_size(result) <= budget:
            return result
    # Exchange boundaries remain valid even when parallel tools finish out of order.
    if turns:
        first = turns[0]
        units = first.get('execution_units', [])
        for count in range(len(units) - 1, 0, -1):
            chosen = units[:count]
            ranges = sorted((start, end, unit['sequence']) for unit in chosen
                            for start, end in unit['text_ranges'])
            lines, updated, position = [], {}, 0
            for start, end, sequence in ranges:
                line = first['timeline'][start:end]
                lines.append(line)
                updated.setdefault(sequence, []).append([position, position + len(line)])
                position += len(line)
            last = chosen[-1]['sequence']
            page = {k: v for k, v in first.items()
                    if k not in {'message_refs', 'dialogue_scope', 'dialogue_truncated',
                                 'detail_reads', 'omitted_tool_calls'}}
            page.update(timeline=''.join(lines), next_after_sequence=last,
                        execution_units=[{'sequence': seq, 'text_ranges': spans}
                                         for seq, spans in updated.items()])
            reads = [r for r in first.get('detail_reads', [])
                     if int(r['tool'].split('.')[0]) <= last]
            if reads:
                page['detail_reads'] = reads
            result = {**value, 'turns': [page], 'truncated': True,
                      'omitted_fields': ['later execution units and turn dialogue'],
                      'evidence_note': 'Continue this turn with turn_id / next_after_sequence before advancing execution_cursor.'}
            if first.get('ordinal') is not None:
                result['next_execution_cursor'] = first['ordinal']
            result['omitted_turns'] = value.get('omitted_turns', 0) + len(turns) - 1
            if encode_size(result) <= budget:
                return result
    # An indivisible Turn exceeds the inline budget. Its full raw response was
    # saved before projection. Do not offer sequence/Turn cursors that skip it.
    result = {key: value[key] for key in ('ok', 'error', 'episode_id', 'result_ref') if key in value}
    result.update(truncated=True, omitted_fields=['execution timeline exceeds inline budget'],
                  evidence_note='执行时间线超出展示预算，未推进执行游标；使用 result_ref 和 read_tool_result 分页读取本次完整快照。')
    return result


def project_episode(result):
    """Idempotent projection for all episode_read modes."""
    if isinstance(result.get('episode'), dict):
        episode = result['episode']
        if 'transcript' in episode:
            return result
        messages = episode.get('messages', [])
        ordinals = sorted({item['ordinal'] for item in messages})[-3:]
        selected = sorted((item for item in messages if item['ordinal'] in ordinals),
                          key=lambda item: (item['ordinal'], item.get('created_at', 0), item.get('id', 0)))
        page = {key: episode[key] for key in (
            'id', 'title', 'status', 'truncated', 'next_before_ordinal', 'next_execution_cursor'
        ) if key in episode}
        if episode.get('turns'):
            page['next_execution_cursor'] = 0
        if len(selected) < len(messages):
            page.update(next_before_ordinal=min(ordinals), truncated=True)
        page['transcript'], refs, clipped = message_page(selected)
        if refs:
            page['message_refs'] = refs
        if clipped:
            page['truncated'] = True
        result['episode'] = page
    elif isinstance(result.get('message'), dict):
        message = result['message']
        if 'transcript' in message:
            return result
        text, refs, clipped = message_page([message])
        result['message'] = {'transcript': text, **(refs[0] if refs else {})}
        if clipped:
            result['message']['truncated'] = True
    elif isinstance(result.get('turns'), list):
        projected = []
        for turn in result['turns']:
            if 'timeline' in turn:
                projected.append(turn)
                continue
            events, reads, message_refs = [], [], []
            dialogue = turn.get('messages', [])
            width = max(1, 6000 // max(1, len(dialogue)))
            dialogue_truncated = False
            for index, message in enumerate(dialogue):
                text, refs, clipped = message_page([message], budget=width)
                if refs:
                    # Number within this Turn, in chronological source order.
                    text = text.replace('[1] ', f'[m{index + 1}] ', 1)
                    message_refs.extend({key: value for key, value in ref.items() if key != 'text_range'}
                                        for ref in refs)
                dialogue_truncated |= clipped
                events.append((message.get('created_at', 0), 0, index, text, None))
            for index, entry in enumerate(turn.get('execution', [])):
                sequence = entry.get('sequence', index)
                recorded = entry.get('recorded_at', 0)
                if entry.get('assistant_text'):
                    # The exchange is stored after the batch. Mark its time as a
                    # recording time; do not pretend it is the model start time.
                    events.append((recorded, sequence, 0,
                                   f'[{entry.get("recorded_time", "unknown")}] ASSISTANT_NOTE [exchange #{sequence}, recorded]: {entry["assistant_text"]}', sequence))
                for call_index, call in enumerate(entry.get('tools', [])):
                    if call['name'] in CONTROL_TOOLS:
                        continue
                    timing = call.get('timing', {})
                    label = f'{sequence}.{call_index + 1}'
                    call_time = timing.get('called_at', recorded)
                    result_time = timing.get('finished_at', recorded)
                    call_stamp = timing.get('called_time', f'recorded {entry.get("recorded_time", "unknown")}')
                    result_stamp = timing.get('finished_time', f'recorded {entry.get("recorded_time", "unknown")}')
                    events.append((call_time, timing.get('call_sequence', sequence), 1,
                                   f'[{call_stamp}] TOOL_CALL #{label} {call["name"]}: {encode(call.get("arguments", {}))}', sequence))
                    outcome = call.get('result', {})
                    if isinstance(outcome, dict):
                        outcome = {key: value for key, value in outcome.items() if key != 'result_ref'}
                    events.append((result_time, timing.get('result_sequence', sequence), 2,
                                   f'[{result_stamp}] TOOL_RESULT #{label}: {encode(outcome)}', sequence))
                    reference = {key: call[key] for key in (
                        'arguments_read', 'arguments_truncated', 'arguments_complete'
                    ) if key in call}
                    if isinstance(call.get('result'), dict) and call['result'].get('result_ref'):
                        reference['result_ref'] = call['result']['result_ref']
                    if reference:
                        reads.append({'tool': label, **reference})
            item = {key: value for key, value in turn.items() if key not in {'messages', 'execution'}}
            lines, units, position = [], {}, 0
            for event in sorted(events, key=lambda event: event[:3]):
                line = event[3] + '\n'
                if event[4] is not None:
                    units.setdefault(event[4], []).append([position, position + len(line)])
                lines.append(line)
                position += len(line)
            item['timeline'] = ''.join(lines)
            if units:
                item['execution_units'] = [{'sequence': seq, 'text_ranges': ranges}
                                           for seq, ranges in sorted(units.items())]
            if message_refs:
                item['message_refs'] = message_refs
                item['dialogue_scope'] = 'turn'
            if dialogue_truncated:
                item['dialogue_truncated'] = True
            if reads:
                item['detail_reads'] = reads
            projected.append(item)
        result['turns'] = projected
    return result
