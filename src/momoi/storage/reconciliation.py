import re
import time


class ReconciliationStore:
    def open_reconciliation(self, turn_id: str, reason: str) -> None:
        with self._db:
            self._open_reconciliation(turn_id, reason, time.time())

    def _open_reconciliation(self, turn_id: str, reason: str, now: float) -> None:
        self._db.execute(
            """INSERT INTO reconciliations
               (turn_id, status, reason, resolution, created_at, updated_at)
               VALUES (?, 'open', ?, '', ?, ?)
               ON CONFLICT(turn_id) DO UPDATE SET status='open', reason=excluded.reason,
                 resolution='', updated_at=excluded.updated_at""",
            (turn_id, reason[:500], now, now),
        )

    def resolve_reconciliation(
        self, turn_prefix: str, resolution: str, *, resume: bool
    ) -> dict[str, object]:
        prefix = turn_prefix.strip()
        resolution = resolution.strip()
        if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise ValueError(
                "turn id prefix must contain at least 8 hexadecimal characters"
            )
        if not resolution:
            raise ValueError("resolution is required")
        rows = self._db.execute(
            """SELECT * FROM reconciliations
               WHERE status='open' AND turn_id LIKE ? ORDER BY created_at""",
            (f"{prefix}%",),
        ).fetchall()
        if not rows:
            raise ValueError("open reconciliation not found")
        if len(rows) > 1:
            raise ValueError("turn id prefix is ambiguous")
        row = rows[0]
        status = "resumed" if resume else "resolved"
        with self._db:
            self._db.execute(
                """UPDATE reconciliations SET status=?, resolution=?, updated_at=?
                   WHERE turn_id=?""",
                (status, resolution[:2000], time.time(), row["turn_id"]),
            )
        return {
            **dict(row),
            "status": status,
            "resolution": resolution[:2000],
        }
