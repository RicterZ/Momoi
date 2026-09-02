import time
from datetime import datetime, timedelta

from ..config.models import ReflectionConfig


class ReflectionScheduleStore:
    def _reflection_slot(
        self, now: float, at: str
    ) -> tuple[str, float, datetime]:
        local = datetime.fromtimestamp(now, self._timezone)
        hour, minute = map(int, at.split(":"))
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled.timestamp() > now:
            scheduled -= timedelta(days=1)
        local_date = (scheduled.date() - timedelta(days=1)).isoformat()
        return local_date, scheduled.timestamp(), scheduled

    def claim_manual_reflection(
        self,
        now: float | None = None,
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        local_date = datetime.fromtimestamp(now, self._timezone).date().isoformat()
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, now, now),
            )
            row = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
            if row is None or row["state"] == "running":
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?,
                   retry_at=NULL, error=NULL WHERE id=?""",
                (now, reflection_id),
            )
            claimed = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
        return dict(claimed) if claimed is not None else None

    def claim_due_reflection(
        self,
        config: ReflectionConfig,
        now: float | None = None,
    ) -> dict[str, object] | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, _ = self._reflection_slot(now, config.at)
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, scheduled_at, now),
            )
            row = self._db.execute(
                """SELECT * FROM reflections
                   WHERE id=? AND state='pending' AND claimed_at IS NULL
                     AND scheduled_at<=? AND COALESCE(retry_at, 0)<=?""",
                (reflection_id, now, now),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?, error=NULL
                   WHERE id=?""",
                (now, reflection_id),
            )
        return dict(row)

    def next_reflection_due_at(
        self,
        config: ReflectionConfig,
        now: float | None = None,
    ) -> float | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, scheduled = self._reflection_slot(
            now, config.at
        )
        row = self._db.execute(
            "SELECT state, retry_at FROM reflections WHERE local_date=?",
            (local_date,),
        ).fetchone()
        if row is None:
            return scheduled_at
        if row["state"] == "pending":
            return max(scheduled_at, float(row["retry_at"] or 0))
        if row["state"] == "running":
            return None
        next_scheduled = scheduled + timedelta(days=1)
        return next_scheduled.timestamp()

    def release_reflection(
        self, local_date: str, error: str, delay_seconds: float = 300
    ) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='pending', claimed_at=NULL,
                   retry_at=?, error=? WHERE local_date=? AND state='running'""",
                (now + delay_seconds, error[:500], local_date),
            )

    def restore_completed_reflection_claim(self, local_date: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL,
                   retry_at=NULL, error=NULL
                   WHERE local_date=? AND state='running'""",
                (local_date,),
            )

