"""Persist actual trigger injections over the last 100 owner messages."""
import hashlib
import json

def record_trigger_injections(store, events, rows, channel):
    events = [event for event in events if not event.delivery_context.get("channel_notice")]
    if not events:
        return False
    db = store._db
    queued = False
    with db:
        for index, event in enumerate(events):
            # A batch injects each memory once, attributed to its final message.
            identifiers = [str(row['id']) for row in rows] if index == len(events) - 1 else []
            db.execute('INSERT OR IGNORE INTO trigger_message_window(event_id,memory_ids_json) VALUES (?,?)',
                       (event.event_id, json.dumps(identifiers)))
        db.execute('DELETE FROM trigger_message_window WHERE sequence NOT IN '
                   '(SELECT sequence FROM trigger_message_window ORDER BY sequence DESC LIMIT 100)')
        counts = {}
        for item in db.execute('SELECT memory_ids_json FROM trigger_message_window'):
            for identifier in set(json.loads(item[0])):
                counts[identifier] = counts.get(identifier, 0) + 1
        for item in db.execute('SELECT memory_id FROM trigger_warning_state WHERE warned=1').fetchall():
            if counts.get(item[0], 0) <= 10:
                db.execute('UPDATE trigger_warning_state SET warned=0 WHERE memory_id=?', (item[0],))
        for row in rows:
            identifier = str(row['id'])
            triggers = row['meta'].get('triggers', [])
            fingerprint = hashlib.sha256(json.dumps(triggers, ensure_ascii=False).encode()).hexdigest()
            previous = db.execute('SELECT fingerprint,warned FROM trigger_warning_state WHERE memory_id=?', (identifier,)).fetchone()
            if counts.get(identifier, 0) <= 10 or (previous and previous[0] == fingerprint and previous[1]):
                continue
            turn_id = 'trigger-warning:' + hashlib.sha256((str(row['id']) + events[-1].event_id).encode()).hexdigest()[:32]
            words = '、'.join(f'“{word}”' for word in triggers)
            text = f'记忆“{row["content"]}”触发词{words}可能过宽导致多次注入。'
            store.begin_turn(turn_id, 'owner', [])
            store.queue_progress(turn_id, 'trigger-warning', [text], channel)
            store.complete_background_turn(turn_id)
            db.execute('INSERT INTO trigger_warning_state VALUES (?,?,1) ON CONFLICT(memory_id) '
                       'DO UPDATE SET fingerprint=excluded.fingerprint,warned=1', (identifier, fingerprint))
            queued = True
    return queued
