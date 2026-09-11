from __future__ import annotations

import sqlite3
import time

from .memory_values import MEMORY_ACTIVATIONS, format_memory


class MemoryInventoryStore:
    def maintenance_memory_inventory(self) -> list[dict[str, object]]:
        self.purge_expired_memories()
        now = time.time()
        rows = self._db.execute(
            """SELECT id, kind, key, content, activation, authority,
                      source_event_id, evidence_quote, importance,
                      created_at, updated_at, expires_at, superseded_by
               FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at>?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.id""",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def purge_expired_memories(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        rows = self._db.execute(
            """SELECT id FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND m.expires_at IS NOT NULL AND m.expires_at <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (now,),
        ).fetchall()
        if not rows:
            return 0
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self._db.execute(
            f"DELETE FROM memory_evidence WHERE memory_id IN ({placeholders})",
            ids,
        )
        self._db.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        self._db.commit()
        return len(ids)

    def _memory_rows(
        self, activation: str, *, now: float | None = None
    ) -> list[sqlite3.Row]:
        if activation not in MEMORY_ACTIVATIONS:
            raise ValueError("invalid memory activation")
        now = time.time() if now is None else now
        return self._db.execute(
            """SELECT id, kind, key, content, activation, importance, updated_at
               FROM memories AS m
               WHERE m.activation=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.id""",
            (activation, now),
        ).fetchall()

    @staticmethod
    def _memory_context(rows: list[sqlite3.Row]) -> str:
        return "\n\n".join(
            format_memory(dict(row))
            for row in rows
        )

    def always_memory_context(self) -> str:
        return self._memory_context(self._memory_rows("always"))

    def has_memory(self, kind: str, key: str) -> bool:
        return (
            self._db.execute(
                """SELECT 1 FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
                (kind, key, time.time()),
            ).fetchone()
            is not None
        )

    def active_memory(self, kind: str, key: str) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT id, kind, key, content, importance FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.id DESC LIMIT 1""",
            (kind, key, time.time()),
        ).fetchone()
        return dict(row) if row else None
