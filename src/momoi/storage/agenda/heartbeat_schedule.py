import time

from ...config.models import HeartbeatConfig, NotificationConfig
from ..core.scheduling import quiet_until


class HeartbeatScheduleStore:
    def ensure_heartbeat(
        self, config: HeartbeatConfig, now: float | None = None
    ) -> None:
        if not config.enabled:
            return
        now = time.time() if now is None else now
        with self._db:
            self._db.execute(
                """UPDATE self_state SET next_heartbeat_at=?, updated_at=?
                   WHERE id=1 AND next_heartbeat_at<=0""",
                (now + config.initial_delay_seconds, now),
            )

    def claim_due_heartbeat(
        self,
        config: HeartbeatConfig,
        notifications: NotificationConfig,
        now: float | None = None,
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
            if row is None or row["heartbeat_claimed_at"] is not None:
                return None
            scheduled_at = float(row["next_heartbeat_at"] or 0)
            claim_kind = "ordinary"
            if not config.enabled or scheduled_at <= 0:
                return None
            if scheduled_at > now:
                return None
            quiet_end = quiet_until(now, self._timezone, notifications)
            if quiet_end > now:
                self._db.execute(
                    "UPDATE self_state SET next_heartbeat_at=?, updated_at=? WHERE id=1",
                    (quiet_end, now),
                )
                return None
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=?,
                   heartbeat_claim_kind=? WHERE id=1""",
                (now, claim_kind),
            )
        claimed = dict(row)
        claimed["heartbeat_claim_kind"] = claim_kind
        claimed["heartbeat_scheduled_at"] = scheduled_at
        return claimed

    def claim_manual_heartbeat(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                "SELECT heartbeat_claimed_at FROM self_state WHERE id=1"
            ).fetchone()
            if row is None or row["heartbeat_claimed_at"] is not None:
                return False
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=?,
                   heartbeat_claim_kind='manual', updated_at=? WHERE id=1""",
                (now, now),
            )
        return True

    def next_heartbeat_due_at(self, enabled: bool) -> float | None:
        row = self._db.execute(
            """SELECT next_heartbeat_at FROM self_state
               WHERE id=1 AND heartbeat_claimed_at IS NULL"""
        ).fetchone()
        if row is None:
            return None
        due = float(row["next_heartbeat_at"] or 0)
        return due if enabled and due > 0 else None

    def release_heartbeat_claim(self, delay_seconds: float) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=NULL,
                   heartbeat_claim_kind=NULL, next_heartbeat_at=?, updated_at=?
                   WHERE id=1""",
                (now + delay_seconds, now),
            )

    def clear_heartbeat_claim(self) -> None:
        with self._db:
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=NULL,
                   heartbeat_claim_kind=NULL WHERE id=1"""
            )

