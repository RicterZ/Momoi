"""Weekly verification of confirmed memories; results only become review items."""
import json
import time

DAY = 86400
VERIFICATION_LIMIT = 15
COOLDOWN_DAYS = 30
STALE_EVIDENCE_DAYS = 60
VOLATILE_KINDS = ('preference', 'cross_event_state', 'profile')
VERDICTS = ('still_valid', 'possibly_outdated', 'conflicts_with', 'insufficient_evidence')
REVIEW_VERDICTS = {'possibly_outdated', 'conflicts_with'}
# Activation changes re-save the same fact and are not new evidence about it.
EVIDENCE_FILTER_SQL = "e.source_event_id NOT LIKE 'dashboard:memory:activation:%'"
FOLLOW_UP_DEPTH = 3
FOLLOW_UP_LIMIT = 6
SAME_TOPIC_LIMIT = 2
EPISODE_MESSAGES = 16


def _topic(memory):
    return (memory['meta']['scope'], memory['kind'], memory['key'].split('.', 1)[0])


class MemoryVerificationStore:
    def _last_evidence_at(self, memory_id):
        return self._db.execute(
            f"""SELECT MAX(COALESCE(v.received_at, e.created_at)) FROM memory_evidence e
                LEFT JOIN events v ON v.id=e.source_event_id
                WHERE e.memory_id=? AND {EVIDENCE_FILTER_SQL}""", (memory_id,),
        ).fetchone()[0]

    def _recent_use(self, memory, now):
        return self._db.execute(
            """WITH RECURSIVE lineage(id) AS (
                   SELECT id FROM memories WHERE kind=? AND key=? AND scope_key=?
                   UNION SELECT m.id FROM memories m JOIN lineage l ON m.superseded_by=l.id
               )
               SELECT COUNT(*) FROM memory_access a JOIN lineage l ON a.memory_id=l.id
               WHERE a.kind<>'always' AND a.created_at>=?""",
            (memory['kind'], memory['key'], memory['meta']['scope'], now - 30 * DAY),
        ).fetchone()[0]

    def memory_verification_candidates(self, *, now=None, limit=VERIFICATION_LIMIT):
        """Order: volatile kinds, then used-but-unsupported, then same-topic pairs."""
        now = time.time() if now is None else now
        memories = self.memories.repository.inventory()
        topics = {}
        for memory in memories:
            topics.setdefault(_topic(memory), []).append(memory)
        ranked = []
        for memory in memories:
            last_checked = self._db.execute(
                'SELECT MAX(created_at) FROM memory_verifications WHERE kind=? AND key=? AND scope_key=?',
                (memory['kind'], memory['key'], memory['meta']['scope']),
            ).fetchone()[0]
            last_evidence = self._last_evidence_at(memory['id']) or memory['updated_at']
            if (last_checked is not None and last_checked > now - COOLDOWN_DAYS * DAY
                    and last_evidence <= last_checked):
                continue
            used = self._recent_use(memory, now)
            related = [item for item in topics[_topic(memory)] if item['id'] != memory['id']]
            if memory['kind'] in VOLATILE_KINDS:
                tier = 0
            elif used and last_evidence < now - STALE_EVIDENCE_DAYS * DAY:
                tier = 1
            elif related:
                tier = 2
            else:
                continue
            ranked.append(((tier, -used, last_evidence, memory['id']), memory, related))
        ranked.sort(key=lambda item: item[0])
        return [{'memory': memory, 'related': related} for _, memory, related in ranked[:limit]]

    def _episode_messages(self, episode_id):
        rows = self._db.execute(
            """SELECT m.id, m.role, m.content, m.created_at FROM episode_turns t
               JOIN messages m ON m.turn_id=t.turn_id
               WHERE t.episode_id=? AND t.relation='primary' AND m.role IN ('user','assistant')
                 AND m.delivery_state<>'failed'
               ORDER BY m.id DESC LIMIT ?""", (episode_id, EPISODE_MESSAGES),
        ).fetchall()
        return [{'id': f"message:{row['id']}", 'role': row['role'],
                 'at': self.context_timestamp(row['created_at']), 'text': row['content'][:400]}
                for row in reversed(rows)]

    def _episode_record(self, episode_id, relation):
        row = self._db.execute(
            'SELECT id, title, narrative_summary, summary, created_at FROM conversation_episodes WHERE id=?',
            (episode_id,),
        ).fetchone()
        return {'id': f"episode:{row['id']}", 'relation': relation, 'title': row['title'],
                'at': self.context_timestamp(row['created_at']),
                'summary': (row['narrative_summary'] or row['summary'] or '')[:600],
                'messages': self._episode_messages(row['id'])}

    def memory_verification_episodes(self, event_ids):
        """Evidence topics, their later topics on the graph, then recent same-topic ones."""
        event_ids = list(dict.fromkeys(event_ids))
        if not event_ids:
            return []
        placeholders = ','.join('?' for _ in event_ids)
        sources = [row[0] for row in self._db.execute(
            f"""SELECT DISTINCT et.episode_id FROM messages m, json_each(m.source_event_ids_json) s
                JOIN episode_turns et ON et.turn_id=m.turn_id
                WHERE s.value IN ({placeholders}) ORDER BY et.episode_id""", event_ids,
        )]
        seen = dict.fromkeys(sources, 'evidence')
        frontier = list(sources)
        follow_ups = 0
        for _ in range(FOLLOW_UP_DEPTH):
            if not frontier or follow_ups >= FOLLOW_UP_LIMIT:
                break
            marks = ','.join('?' for _ in frontier)
            # Edges point from the newer topic to the older topic it continues or revises.
            rows = self._db.execute(
                f"""SELECT r.source_episode_id, r.relation FROM episode_relations r
                    JOIN conversation_episodes s ON s.id=r.source_episode_id
                    WHERE r.target_episode_id IN ({marks}) AND r.relation IN ('follows_up','revises')
                      AND r.source_summary_ordinal=s.summarized_through_ordinal
                    ORDER BY r.relation='revises' DESC, s.created_at, r.source_episode_id""", frontier,
            ).fetchall()
            frontier = []
            for row in rows:
                if row[0] in seen or follow_ups >= FOLLOW_UP_LIMIT:
                    continue
                seen[row[0]] = row[1]
                frontier.append(row[0])
                follow_ups += 1
        if sources:
            marks = ','.join('?' for _ in seen)
            rows = self._db.execute(
                f"""SELECT DISTINCT c.id FROM conversation_episodes c, json_each(c.topics_json) t
                    WHERE t.type='text' AND c.id NOT IN ({marks})
                      AND c.created_at >= (SELECT MIN(created_at) FROM conversation_episodes WHERE id IN ({marks}))
                      AND t.value IN (SELECT o.value FROM conversation_episodes e, json_each(e.topics_json) o
                                      WHERE e.id IN ({marks}) AND o.type='text')
                    ORDER BY c.created_at DESC LIMIT ?""", (*seen, *seen, *seen, SAME_TOPIC_LIMIT),
            ).fetchall()
            for row in rows:
                seen[row[0]] = 'same_topic'
        return [self._episode_record(identifier, relation) for identifier, relation in seen.items()]

    def memory_verification_item(self, memory, related):
        evidence = self._db.execute(
            f"""SELECT e.id, e.source_event_id, COALESCE(v.content, e.quote) AS text,
                       COALESCE(v.received_at, e.created_at) AS at
                FROM memory_evidence e LEFT JOIN events v ON v.id=e.source_event_id
                WHERE e.memory_id=? AND {EVIDENCE_FILTER_SQL} ORDER BY at, e.id""", (memory['id'],),
        ).fetchall()
        return {
            'memory': {'id': memory['id'], 'kind': memory['kind'], 'key': memory['key'],
                       'content': memory['content'], 'updated_at': self.context_timestamp(memory['updated_at'])},
            'evidence': [{'id': f"evidence:{row['id']}", 'at': self.context_timestamp(row['at']),
                          'text': row['text'][:800]} for row in evidence],
            'later_episodes': self.memory_verification_episodes([row['source_event_id'] for row in evidence]),
            'related_memories': [{key: item[key] for key in ('id', 'kind', 'key', 'content')} for item in related],
        }

    def memory_verification_source(self, period_end, *, now=None):
        items = [self.memory_verification_item(row['memory'], row['related'])
                 for row in self.memory_verification_candidates(now=now)]
        return {'period_end': period_end, 'items': items}

    def memory_verification_run(self, period_end):
        row = self._db.execute('SELECT * FROM memory_verification_runs WHERE period_end=?', (period_end,)).fetchone()
        return dict(row) if row else None

    def _queue_memory_verification(self, period_end, now):
        """Caller owns the weekly reflection transaction."""
        self._db.execute(
            "INSERT OR IGNORE INTO memory_verification_runs(period_end,state,created_at) VALUES (?,'pending',?)",
            (period_end, now),
        )

    def claim_due_memory_verification(self, config, now=None):
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        with self.transaction():
            if self._db.execute("SELECT 1 FROM memory_verification_runs WHERE state='running'").fetchone():
                return None
            row = self._db.execute(
                "SELECT period_end, input_json FROM memory_verification_runs WHERE state='pending' "
                "AND COALESCE(retry_at,0)<=? ORDER BY period_end LIMIT 1", (now,),
            ).fetchone()
            if row is None:
                return None
            source = row['input_json'] or json.dumps(
                self.memory_verification_source(row['period_end'], now=now), ensure_ascii=False)
            self._db.execute(
                "UPDATE memory_verification_runs SET state='running', claimed_at=?, input_json=?, error=NULL "
                "WHERE period_end=?", (now, source, row['period_end']),
            )
        return self.memory_verification_run(row['period_end'])

    def next_memory_verification_due_at(self, config):
        if not config.enabled:
            return None
        return self._db.execute(
            "SELECT MIN(COALESCE(retry_at, created_at)) FROM memory_verification_runs WHERE state='pending'",
        ).fetchone()[0]

    def release_memory_verification(self, period_end, error, delay_seconds=900):
        with self.transaction():
            self._db.execute(
                "UPDATE memory_verification_runs SET state='pending', claimed_at=NULL, retry_at=?, error=? "
                "WHERE period_end=? AND state='running'",
                (time.time() + delay_seconds, error[:500], period_end),
            )

    def commit_memory_verification(self, period_end, turn_id, result):
        now = time.time()
        with self.transaction():
            run = self.memory_verification_run(period_end)
            if run is None or run['state'] != 'running':
                raise ValueError('memory_verification_not_running')
            items = {item['memory']['id']: item for item in json.loads(run['input_json'])['items']}
            validate_memory_verification(items, result['items'])
            snapshots = self.memories.snapshots(list(items))
            for finding in result['items']:
                memory = items[finding['memory_id']]['memory']
                snapshot = snapshots.get(finding['memory_id'])
                identity = self._db.execute(
                    'SELECT kind, key, scope_key FROM memories WHERE id=?', (finding['memory_id'],),
                ).fetchone()
                pending = finding['verdict'] in REVIEW_VERDICTS and snapshot is not None
                self._db.execute(
                    """INSERT INTO memory_verifications
                       (period_end,memory_id,kind,key,scope_key,verdict,conflicts_with,reason,
                        citations_json,snapshot_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (period_end, memory['id'], identity['kind'], identity['key'], identity['scope_key'],
                     finding['verdict'], finding.get('conflicts_with'), finding['reason'],
                     json.dumps(finding['citations'], ensure_ascii=False),
                     json.dumps(snapshot or {}, ensure_ascii=False, sort_keys=True),
                     'pending' if pending else 'recorded', now),
                )
            changed = self._db.execute(
                "UPDATE memory_verification_runs SET state='completed', claimed_at=NULL, retry_at=NULL, "
                "error=NULL, result_json=?, completed_at=? WHERE period_end=? AND state='running'",
                (json.dumps(result, ensure_ascii=False), now, period_end),
            )
            if changed.rowcount != 1:
                raise ValueError('memory_verification_not_running')
            self._db.execute(
                "UPDATE turns SET state='completed', stage='completed', updated_at=? WHERE id=?", (now, turn_id),
            )

    def memory_verification_reviews(self):
        items = []
        for row in self._db.execute(
            "SELECT * FROM memory_verifications WHERE status='pending' ORDER BY id",
        ).fetchall():
            snapshot = json.loads(row['snapshot_json'])
            if self.memories.snapshots([row['memory_id']]).get(row['memory_id']) != snapshot:
                continue
            conflict = self.memories.snapshots([row['conflicts_with']]).get(row['conflicts_with']) \
                if row['conflicts_with'] is not None else None
            items.append({
                'id': row['id'], 'memory_id': row['memory_id'], 'kind': snapshot['kind'],
                'key': snapshot['key'], 'content': snapshot['content'], 'verdict': row['verdict'],
                'reason': row['reason'], 'citations': json.loads(row['citations_json']),
                'conflicts_with': {'id': conflict['id'], 'content': conflict['content']} if conflict else None,
                'created_at': row['created_at'],
            })
        return {'items': items}

    def keep_verified_memory(self, identifier):
        """The owner confirms the memory still holds; edits and deletes use the memory API."""
        with self.transaction():
            changed = self._db.execute(
                "UPDATE memory_verifications SET status='kept', reviewed_at=? WHERE id=? AND status='pending'",
                (time.time(), identifier),
            )
            if changed.rowcount != 1:
                raise LookupError('verification not found')
        return {'ok': True, 'status': 'kept'}


def validate_memory_verification(items, findings):
    """Each sampled memory gets one verdict; citations must quote the supplied material."""
    if sorted(finding['memory_id'] for finding in findings) != sorted(items):
        raise ValueError('verification must cover each memory exactly once')
    for finding in findings:
        item = items[finding['memory_id']]
        verdict = finding['verdict']
        conflict = finding.get('conflicts_with')
        if (verdict == 'conflicts_with') != (conflict is not None):
            raise ValueError(f"memory {finding['memory_id']}: conflicts_with requires a conflicting memory id")
        if conflict is not None and conflict not in {row['id'] for row in item['related_memories']}:
            raise ValueError(f"memory {finding['memory_id']}: conflicts_with must name a related memory")
        if verdict != 'insufficient_evidence' and not finding['citations']:
            raise ValueError(f"memory {finding['memory_id']}: cite the supporting or contradicting text")
        texts = {row['id']: row['text'] for row in item['evidence']}
        texts.update((message['id'], message['text'])
                     for episode in item['later_episodes'] for message in episode['messages'])
        texts.update((f"memory:{row['id']}", row['content']) for row in item['related_memories'])
        for citation in finding['citations']:
            text = texts.get(citation['source'])
            if text is None:
                raise ValueError(f"memory {finding['memory_id']}: unknown citation source {citation['source']}")
            if citation['quote'].strip() not in text:
                raise ValueError(f"memory {finding['memory_id']}: quote must be copied verbatim from {citation['source']}")
