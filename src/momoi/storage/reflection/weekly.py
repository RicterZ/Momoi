"""Weekly review claims and immutable daily-observation snapshots."""
import json
import time
from datetime import date, datetime, timedelta


from .candidates import ReflectionCandidateStore


class WeeklyReflectionStore(ReflectionCandidateStore):
    def _weekly_reflection_slot(self, now):
        local = datetime.fromtimestamp(now, self._timezone)
        scheduled = (local - timedelta(days=(local.weekday() - 6) % 7)).replace(
            hour=5, minute=0, second=0, microsecond=0,
        )
        if scheduled.timestamp() > now:
            scheduled -= timedelta(days=7)
        return scheduled

    def weekly_reflection_source(self, period_end):
        end = date.fromisoformat(period_end)
        days = []
        for offset in range(7, 0, -1):
            day = (end - timedelta(days=offset)).isoformat()
            reflection = self.reflection(day)
            observations = []
            if reflection and reflection['state'] == 'completed':
                for row in self._db.execute(
                    """SELECT m.id, m.kind, m.key, m.content, m.evidence, m.confidence, m.triggers_json
                       FROM reflection_memories m WHERE m.source_reflection_id=?
                       AND NOT EXISTS (SELECT 1 FROM reflection_memory_tombstones t
                                       WHERE t.kind=m.kind AND t.key=m.key)
                       ORDER BY m.id""", (reflection['id'],),
                ).fetchall():
                    item = dict(row)
                    item["triggers"] = json.loads(item.pop("triggers_json"))
                    item['id'] = f"observation:{item['id']}"
                    item['key'] = item['key'].removeprefix(day + '.')
                    observations.append(item)
            days.append({'date': day, 'state': reflection['state'] if reflection else 'missing',
                         'observations': observations})
        return {'period_start': days[0]['date'], 'period_end': period_end, 'days': days,
                'previous_candidates': self.reflection_candidates(end=period_end, include_seeds=True)}

    def weekly_reflection(self, period_end):
        row = self._db.execute(
            'SELECT * FROM weekly_reflections WHERE period_end=?', (period_end,),
        ).fetchone()
        return dict(row) if row else None

    def claim_due_weekly_reflection(self, config, now=None):
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        scheduled = self._weekly_reflection_slot(now)
        with self._db:
            if self._db.execute("SELECT 1 FROM weekly_reflections WHERE state='running' OR (state='pending' AND COALESCE(retry_at,0)>?)", (now,)).fetchone():
                return None
            pending = self._db.execute(
                "SELECT period_end FROM weekly_reflections WHERE state='pending' "
                "AND scheduled_at<=? AND COALESCE(retry_at,0)<=? ORDER BY scheduled_at LIMIT 1",
                (now, now),
            ).fetchone()
            end = pending['period_end'] if pending else scheduled.date().isoformat()
            start = (date.fromisoformat(end) - timedelta(days=7)).isoformat()
            existing = self.weekly_reflection(end)
            if existing and (existing['state'] != 'pending' or (existing['retry_at'] or 0) > now):
                return None
            # Let an already queued daily review finish before freezing its week.
            if self._db.execute(
                "SELECT 1 FROM reflections WHERE local_date>=? AND local_date<? "
                "AND state IN ('pending','running') LIMIT 1", (start, end),
            ).fetchone():
                return None
            if existing is None:
                source = self.weekly_reflection_source(end)
                self._db.execute(
                    "INSERT INTO weekly_reflections "
                    "(period_end, scheduled_at, state, input_json, created_at) "
                    "VALUES (?, ?, 'pending', ?, ?)",
                    (end, scheduled.timestamp(), json.dumps(source, ensure_ascii=False), now),
                )
            self._db.execute(
                "UPDATE weekly_reflections SET state='running', claimed_at=?, error=NULL "
                "WHERE period_end=?", (now, end),
            )
        return self.weekly_reflection(end)

    def next_weekly_reflection_due_at(self, config, now=None):
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        pending = self._db.execute(
            "SELECT MIN(MAX(scheduled_at, COALESCE(retry_at,0))) FROM weekly_reflections WHERE state='pending'",
        ).fetchone()[0]
        scheduled = self._weekly_reflection_slot(now)
        row = self.weekly_reflection(scheduled.date().isoformat())
        if row is None:
            return min(scheduled.timestamp(), pending) if pending is not None else scheduled.timestamp()
        if row['state'] == 'pending':
            return pending
        if row['state'] == 'running':
            return pending
        next_at = (scheduled + timedelta(days=7)).timestamp()
        return min(next_at, pending) if pending is not None else next_at

    def release_weekly_reflection(self, period_end, error, delay_seconds=900):
        with self._db:
            self._db.execute(
                "UPDATE weekly_reflections SET state='pending', claimed_at=NULL, "
                "retry_at=?, error=? WHERE period_end=? AND state='running'",
                (time.time() + delay_seconds, error[:500], period_end),
            )

    def commit_weekly_reflection(self, period_end, turn_id, result):
        now = time.time()
        with self.transaction():
            record = self.weekly_reflection(period_end)
            if record is None or record['state'] != 'running':
                raise ValueError('weekly_reflection_not_running')
            self.apply_reflection_candidates(json.loads(record['input_json']), result['findings'])
            changed = self._db.execute(
                "UPDATE weekly_reflections SET state='completed', claimed_at=NULL, "
                "retry_at=NULL, error=NULL, result_json=?, completed_at=? "
                "WHERE period_end=? AND state='running'",
                (json.dumps(result, ensure_ascii=False), now, period_end),
            )
            if changed.rowcount != 1:
                raise ValueError('weekly_reflection_not_running')
            self._db.execute(
                "UPDATE turns SET state='completed', stage='completed', updated_at=? WHERE id=?",
                (now, turn_id),
            )
