"""Weekly activation suggestions; confirmed memories change only on explicit review."""
import json
import time
from uuid import uuid4

from ...memory.text import estimate_tokens
from ...memory.writing.validation import ALWAYS_MEMORY_KINDS

DAY = 86400
VOLATILE_KINDS = {'preference', 'cross_event_state', 'profile'}


class MemoryActivationStore:
    def always_memory_tokens(self):
        text = self.always_memory_context()
        return estimate_tokens(text) if text else 0

    def memory_activation_candidates(self, *, now=None):
        now = time.time() if now is None else now
        started = self._db.execute('SELECT started_at FROM memory_usage_observation WHERE id=1').fetchone()[0]
        candidates = []
        for memory in self.memories.repository.inventory():
            if memory['activation'] not in {'always', 'recall'} or memory['meta']['scope']:
                continue
            usage = self._db.execute('''
                WITH RECURSIVE lineage(id) AS (
                    SELECT id FROM memories WHERE kind=? AND key=? AND scope_key=''
                    UNION SELECT m.id FROM memories m JOIN lineage l ON m.superseded_by=l.id
                )
                SELECT
                  (SELECT COUNT(*) FROM memory_access a JOIN lineage l ON a.memory_id=l.id
                   WHERE a.kind IN ('recalled','read') AND a.created_at>=? AND a.created_at<=?) AS hits,
                  (SELECT COUNT(DISTINCT e.episode_id) FROM memory_access a
                   JOIN lineage l ON a.memory_id=l.id JOIN episode_turns e ON e.turn_id=a.turn_id
                   WHERE e.relation='primary' AND a.kind IN ('recalled','read')
                   AND a.created_at>=? AND a.created_at<=?) AS topics,
                  (SELECT MAX(u.created_at) FROM memory_reply_usage u JOIN lineage l ON u.memory_id=l.id
                   WHERE u.created_at<=?) AS last_used,
                  (SELECT MIN(m.created_at) FROM memories m JOIN lineage l ON m.id=l.id
                   WHERE m.activation='always' AND m.created_at > COALESCE(
                       (SELECT MAX(v.created_at) FROM memories v JOIN lineage l2 ON v.id=l2.id
                        WHERE v.activation!='always'), 0)) AS always_since
            ''', (memory['kind'], memory['key'], now - 30*DAY, now, now - 30*DAY, now, now)).fetchone()
            reason = None
            if (memory['activation'] == 'recall' and memory['kind'] in ALWAYS_MEMORY_KINDS
                    and usage['hits'] >= 5 and usage['topics'] >= 3):
                target, reason = 'always', '最近 30 天召回或读取至少 5 次，覆盖至少 3 个话题'
            elif memory['activation'] == 'always':
                if memory['kind'] in VOLATILE_KINDS:
                    target, reason = 'recall', '内容属于易变类别，请核对是否仍需每轮常驻'
                elif (max(started, usage['always_since'] or now) <= now - 60*DAY
                      and (usage['last_used'] is None or usage['last_used'] < now - 60*DAY)):
                    target, reason = 'recall', '完整观察 60 天，回复中没有记录到使用或引用；请人工核对'
            if reason:
                candidates.append({'memory_id': memory['id'], 'target': target, 'reason': reason,
                                   'hits': usage['hits'], 'topics': usage['topics']})
        return candidates

    def generate_memory_activation_suggestions(self, *, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            candidates = self.memory_activation_candidates(now=now)
            current = {(item['memory_id'], item['target']) for item in candidates}
            for row in self._db.execute("SELECT id,memory_id,target FROM memory_activation_suggestions WHERE status='pending'"):
                if (row['memory_id'], row['target']) not in current:
                    self._db.execute("UPDATE memory_activation_suggestions SET status='stale' WHERE id=?", (row['id'],))
            snapshots = self.memories.snapshots([item['memory_id'] for item in candidates])
            for item in candidates:
                self._db.execute('''INSERT OR IGNORE INTO memory_activation_suggestions
                    (memory_id,target,reason,snapshot_json,created_at) VALUES (?,?,?,?,?)''',
                    (item['memory_id'], item['target'], item['reason'],
                     json.dumps(snapshots[item['memory_id']], ensure_ascii=False, sort_keys=True), now))
        return candidates

    def memory_activation_suggestions(self):
        items = []
        for row in self._db.execute("SELECT * FROM memory_activation_suggestions WHERE status='pending' ORDER BY id"):
            item = dict(row)
            snapshot = json.loads(item.pop('snapshot_json'))
            if self.memories.snapshots([item['memory_id']]).get(item['memory_id']) != snapshot:
                continue
            item.update(content=snapshot['content'], kind=snapshot['kind'], key=snapshot['key'])
            items.append(item)
        return {'items': items, 'always_tokens': self.always_memory_tokens(),
                'always_token_budget': self.memories.repository.always_token_budget,
                'always_memories': self.memories.repository.rows('always')}

    def review_memory_activation_suggestion(self, identifier, *, approve, demote_ids=()):
        if not isinstance(approve, bool) or not isinstance(demote_ids, (list, tuple)) or any(type(x) is not int for x in demote_ids):
            raise ValueError('invalid activation review')
        if len(set(demote_ids)) != len(demote_ids):
            raise ValueError('duplicate demote_ids')
        with self.transaction():
            row = self._db.execute("SELECT * FROM memory_activation_suggestions WHERE id=? AND status='pending'", (identifier,)).fetchone()
            if row is None:
                raise LookupError('suggestion not found')
            if not approve:
                self._db.execute("UPDATE memory_activation_suggestions SET status='rejected' WHERE id=?", (identifier,))
                return {'ok': True, 'status': 'rejected'}
            repository = self.memories.repository
            snapshot = json.loads(row['snapshot_json'])
            repository.validate_snapshots({row['memory_id']: snapshot})
            if demote_ids and row['target'] != 'always':
                raise ValueError('demote_ids requires a promotion')
            demotions = repository.snapshots(list(demote_ids))
            if len(demotions) != len(demote_ids) or any(item['activation'] != 'always' for item in demotions.values()):
                raise ValueError('demote_ids must identify current always memories')
            source = {'event_id': 'dashboard:memory:activation:' + uuid4().hex, 'quote': '用户确认常驻记忆升降级建议'}
            now = time.time()
            for item in demotions.values():
                repository.replace(item['id'], item['content'], 'recall', None, source, updated_at=now)
            new_id = repository.replace(snapshot['id'], snapshot['content'], row['target'], None, source, updated_at=now)
            self._db.execute("UPDATE memory_activation_suggestions SET status='approved' WHERE id=?", (identifier,))
            return {'ok': True, 'status': 'approved', 'memory_id': new_id}
