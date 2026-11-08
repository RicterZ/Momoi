"""Unconfirmed observations; Dashboard admission queues private memory review."""
import calendar
import hashlib
import json
import time
from datetime import date, datetime

from ...memory.storage.transactions import transaction
from ...memory.metadata import validate_triggers
from ...models import IncomingMessage, TurnDraft


def month_start(end):
    # TODO: Design longer-term evidence accumulation; a rolling month misses slow patterns.
    value = date.fromisoformat(end)
    year, month = (value.year, value.month - 1) if value.month > 1 else (value.year - 1, 12)
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1])).isoformat()


def current_events(events, end):
    return [event for event in events if month_start(end) <= event['date'] <= end]


def candidate_status(events, conflicts):
    if conflicts:
        return 'blocked'
    return 'pending' if len(events) >= 5 else 'observation'


class ReflectionCandidateStore:
    def reflection_candidates(self, *, end=None, include_seeds=False):
        end = end or datetime.now(self._timezone).date().isoformat()
        result = []
        for row in self._db.execute("""SELECT c.*, b.state AS review_state, b.error AS review_error,
                                           b.result_json AS review_result
                                      FROM reflection_candidates c
                                      LEFT JOIN memory_operation_batches b ON b.id=c.memory_operation_id
                                      WHERE c.state='active' ORDER BY c.updated_at DESC,c.id DESC"""):
            item = dict(row)
            item['triggers'] = json.loads(item.pop('triggers_json'))
            item['events'] = current_events(json.loads(item.pop('events_json')), end)
            item['conflicts'] = current_events(json.loads(item.pop('conflicts_json')), end)
            item['count'] = len(item['events'])
            item['status'] = candidate_status(item['events'], item['conflicts'])
            decisions = json.loads(item.pop('review_result') or '[]')
            item['review_reason'] = decisions[0]['reason'] if decisions else item['review_error']
            if item['count'] >= (1 if include_seeds else 2):
                result.append(item)
        return result

    def apply_reflection_candidates(self, source, findings):
        """Called in the weekly commit transaction, using frozen source references."""
        end = source['period_end']
        previous = source.get('previous_candidates', [])
        records = {}
        for day in source['days']:
            for row in day['observations']:
                records[row['id']] = {'date': day['date'], 'sources': [row['id']]}
        for topic in previous:
            for event in topic['events'] + topic['conflicts']:
                records[event['id']] = event
        seen = set()
        for finding in findings:
            key = finding['key'].strip()
            if key in seen:
                raise ValueError('duplicate_candidate_key')
            seen.add(key)
            old = self._db.execute('SELECT * FROM reflection_candidates WHERE key=?', (key,)).fetchone()
            if old and old['state'] != 'active':
                continue  # A frozen model request cannot revive a deleted/admitted candidate.
            if old and self._reflection_candidate_review(old) in ('pending', 'running'):
                continue
            events, used = [], set()
            for group in finding['events']:
                refs = group['refs']
                if not refs or not set(refs) <= records.keys():
                    raise ValueError('unknown_observation_reference')
                sources = sorted({s for ref in refs for s in records[ref]['sources']})
                if used.intersection(sources):
                    raise ValueError('same_event_counted_twice')
                used.update(sources)
                when = min(records[ref]['date'] for ref in refs)
                identifier = 'event:' + hashlib.sha256('\n'.join(sources).encode()).hexdigest()[:24]
                events.append({'id': identifier, 'date': when, 'sources': sources, 'summary': group['summary']})
            conflicts = []
            for ref in finding['conflicts']:
                if ref not in records or used.intersection(records[ref]['sources']):
                    raise ValueError('invalid_conflict_reference')
                conflicts.append({**records[ref], 'id': ref, 'summary': '存在未解决的冲突'})
            if old:
                for conflict in current_events(json.loads(old['conflicts_json']), end):
                    if used.intersection(conflict['sources']):
                        raise ValueError('conflict_reused_as_support')
                    if conflict['id'] not in {c['id'] for c in conflicts}:
                        conflicts.append(conflict)
                # Prior support must be retained/grouped explicitly; omission is not deletion.
                for event in current_events(json.loads(old['events_json']), end):
                    if not used.intersection(event['sources']) and not any(set(event['sources']) & set(c['sources']) for c in conflicts):
                        events.append(event)
            events = current_events(events, end)
            content = old['content'] if old and old['edited'] else finding['content'].strip()
            triggers = (json.loads(old['triggers_json']) if old and old['edited']
                        else validate_triggers(finding.get('triggers', json.loads(old['triggers_json']) if old else [])))
            now = time.time()
            self._db.execute(
                """INSERT INTO reflection_candidates(key,kind,content,events_json,conflicts_json,updated_at,triggers_json)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                   kind=excluded.kind,content=excluded.content,triggers_json=excluded.triggers_json,events_json=excluded.events_json,
                   conflicts_json=excluded.conflicts_json,updated_at=excluded.updated_at,revision=revision+1""",
                (key, finding['kind'], content, json.dumps(events, ensure_ascii=False),
                 json.dumps(conflicts, ensure_ascii=False), now, json.dumps(triggers, ensure_ascii=False)),
            )

    def _reflection_candidate_review(self, row):
        batch = self._db.execute('SELECT state FROM memory_operation_batches WHERE id=?',
                                 (row['memory_operation_id'],)).fetchone()
        return batch['state'] if batch else None

    def change_reflection_candidate(self, identifier, revision, *, content=None, delete=False, admit=False):
        with transaction(self._db):
            row = self._db.execute('SELECT * FROM reflection_candidates WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise LookupError('candidate_not_found')
            operation_id = f'dashboard:reflection:{identifier}:{revision}'
            review_state = self._reflection_candidate_review(row)
            if admit and row['memory_operation_id'] == operation_id:
                return {'ok': True, 'state': review_state, 'operation_id': operation_id,
                        'memory_id': row['memory_id']}
            # Preserve receipts for candidates admitted before private review was introduced.
            if admit and row['state'] == 'admitted' and revision == row['revision'] - 1:
                return {'ok': True, 'memory_id': row['memory_id']}
            if row['state'] != 'active' or row['revision'] != revision:
                raise ValueError('candidate_changed_refresh')
            if review_state in ('pending', 'running'):
                raise ValueError('candidate_review_in_progress')
            now = time.time()
            if admit:
                end = datetime.now(self._timezone).date().isoformat()
                events = current_events(json.loads(row['events_json']), end)
                conflicts = current_events(json.loads(row['conflicts_json']), end)
                if len(events) < 5 or conflicts:
                    raise ValueError('candidate_not_ready')
                evidence = IncomingMessage(operation_id, operation_id, row['content'], now, now)
                draft = TurnDraft(memory_operations=[{
                    'id': 'admit', 'type': 'add', 'scope': '', 'content': row['content'],
                    'event_id': operation_id, 'evidence': row['content'],
                }], memory_conversation=[{'role': 'user', 'content':
                    'Dashboard 人工提交复盘候选，请审核重复和冲突后决定是否写入。候选材料：' +
                    json.dumps({'kind': row['kind'], 'content': row['content'],
                                'triggers': json.loads(row['triggers_json']), 'events': events}, ensure_ascii=False)}])
                self._db.execute(
                    """INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,stage,started_at,updated_at)
                       VALUES (?,'autonomous','memory_operation',?,'completed','completed',?,?)""",
                    (operation_id, json.dumps([f'reflection-candidate:{identifier}']), now, now),
                )
                self._queue_memory_operations(operation_id, draft, [evidence], now)
                self._db.execute('UPDATE reflection_candidates SET memory_operation_id=?,revision=revision+1,updated_at=? WHERE id=?',
                                 (operation_id, now, identifier))
                return {'ok': True, 'state': 'pending', 'operation_id': operation_id, 'memory_id': None}
            if delete:
                self._db.execute("UPDATE reflection_candidates SET state='deleted',revision=revision+1,updated_at=? WHERE id=?", (now, identifier))
            else:
                if not isinstance(content, str) or not 1 <= len(content.strip()) <= 1000:
                    raise ValueError('content must contain between 1 and 1000 characters')
                self._db.execute('UPDATE reflection_candidates SET content=?,edited=1,revision=revision+1,updated_at=? WHERE id=?', (content.strip(), now, identifier))
            return {'ok': True}
