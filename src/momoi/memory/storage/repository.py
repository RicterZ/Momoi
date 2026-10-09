from __future__ import annotations

import sqlite3
import time
from typing import cast

from .records import ActiveMemory, InventoryMemory, MEMORY_ACTIVATIONS, memory_snapshot_fingerprint
from .transactions import transaction


class MemoryRepository:
    """Uses a caller-owned connection with sqlite3.Row and an existing schema.

    Mutations compose through savepoints inside an existing transaction, or
    commit their own transaction when used alone. Inputs are validated commands;
    authentication and source-text verification belong to the caller. Connection
    lifetime and schema migrations remain the caller's responsibility.
    """
    def __init__(self, database: sqlite3.Connection) -> None:
        self._db = database

    def validate_snapshots(
        self, snapshots: dict[int, dict[str, object]],
    ) -> dict[int, dict[str, object]]:
        current = self.snapshots(list(snapshots))
        if set(current) != set(snapshots) or any(
            memory_snapshot_fingerprint(current[key])
            != memory_snapshot_fingerprint(snapshot)
            for key, snapshot in snapshots.items()
        ):
            raise ValueError("memory_snapshot_changed")
        return current

    def forget(self, memory, source, *, now: float, require_unique=False) -> None:
        with transaction(self._db):
            if require_unique and self._db.execute(
                """SELECT 1 FROM memories
                   WHERE kind=? AND key=? AND id<>?
                     AND superseded_by IS NULL LIMIT 1""",
                (memory["kind"], memory["key"], memory["id"]),
            ).fetchone() is not None:
                raise ValueError("memory_maintenance_tombstone_conflict")
            self._db.execute(
                """INSERT INTO memory_tombstones
                   (kind,key,source_event_id,evidence_quote,created_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET
                   source_event_id=excluded.source_event_id,
                   evidence_quote=excluded.evidence_quote,created_at=excluded.created_at""",
                (memory["kind"], memory["key"], source["event_id"], source["quote"], now),
            )

    def replace(self, memory_id, content, activation, expires_at, source, *, updated_at) -> None:
        """Update a reviewed record in place; source evidence is checked by the caller."""
        with transaction(self._db):
            self._db.execute(
                """UPDATE memories SET content=?, activation=?, expires_at=?,
                   source_event_id=?, evidence_quote=?, updated_at=?
                   WHERE id=? AND superseded_by IS NULL""",
                (content, activation, expires_at, source["event_id"], source["quote"],
                 updated_at, memory_id),
            )

    def merge(self, survivor_id, source_ids, content, activation, expires_at, events) -> None:
        """Keep the survivor ID and transfer evidence from merged records."""
        newest = max(events, key=lambda item: float(item["occurred_at"]))
        with transaction(self._db):
            for source_id in source_ids:
                self._db.execute(
                    """INSERT OR IGNORE INTO memory_evidence
                       (memory_id, source_event_id, quote, created_at)
                       SELECT ?, source_event_id, quote, created_at
                       FROM memory_evidence WHERE memory_id=?""",
                    (survivor_id, source_id),
                )
                self._db.execute(
                    "UPDATE memories SET superseded_by=? WHERE id=? AND superseded_by IS NULL",
                    (survivor_id, source_id),
                )
            for event in events:
                self.add_evidence(survivor_id, str(event["id"]), str(event["content"]),
                                  float(event["occurred_at"]))
            self.replace(
                survivor_id, content, activation, expires_at,
                {"event_id": newest["id"], "quote": newest["content"]},
                updated_at=newest["occurred_at"],
            )

    def inventory(self) -> list[InventoryMemory]:
        self.purge_expired()
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
        return [cast(InventoryMemory, dict(row)) for row in rows]

    def purge_expired(self, *, now: float | None = None) -> int:
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
        with transaction(self._db):
            self._db.execute(
                f"DELETE FROM memory_evidence WHERE memory_id IN ({placeholders})",
                ids,
            )
            self._db.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})", ids
            )
        return len(ids)

    def rows(
        self, activation: str, *, now: float | None = None
    ) -> list[dict[str, object]]:
        if activation not in MEMORY_ACTIVATIONS:
            raise ValueError("invalid memory activation")
        now = time.time() if now is None else now
        rows = self._db.execute(
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
        return [dict(row) for row in rows]

    def has(self, kind: str, key: str) -> bool:
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

    def active(self, kind: str, key: str) -> ActiveMemory | None:
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
        return cast(ActiveMemory, dict(row)) if row else None

    def snapshots(self, ids: list[int]) -> dict[int, dict[str, object]]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.execute(
            f"""SELECT * FROM memories AS m WHERE m.id IN ({placeholders})
                AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at>?)
                AND NOT EXISTS (SELECT 1 FROM memory_tombstones t WHERE t.kind=m.kind AND t.key=m.key)
                ORDER BY m.id""",
            (*ids, time.time()),
        ).fetchall()
        return {int(row["id"]): dict(row) for row in rows}


    def add_evidence(
        self,
        memory_id: int,
        source_event_id: str,
        quote: str,
        now: float,
    ) -> None:
        with transaction(self._db):
            self._db.execute(
                """INSERT OR IGNORE INTO memory_evidence
                   (memory_id, source_event_id, quote, created_at)
                   VALUES (?, ?, ?, ?)""",
                (memory_id, source_event_id, quote, now),
            )

    def write(self, memory, target_ids, source, evidence, *, now: float) -> int:
        """Replace the supplied versions, carrying forward their evidence."""
        with transaction(self._db):
            existing = self._db.execute(
                """SELECT id FROM memories WHERE kind=? AND key=? AND superseded_by IS NULL
                   AND (expires_at IS NULL OR expires_at>?)""",
                (memory["kind"], memory["key"], now),
            ).fetchall()
            tombstone = self._db.execute(
                "SELECT * FROM memory_tombstones WHERE kind=? AND key=?",
                (memory["kind"], memory["key"]),
            ).fetchone()
            hidden_ids = []
            if tombstone is not None:
                # Re-adding a fact must not unhide its previously deleted versions.
                hidden_ids = [int(row["id"]) for row in existing]
                self._db.execute(
                    "DELETE FROM memory_tombstones WHERE kind=? AND key=?",
                    (memory["kind"], memory["key"]),
                )
            elif any(int(row["id"]) not in target_ids for row in existing):
                raise ValueError(
                    "memory_key_conflict: include the existing target or choose the correct distinct key"
                )
            cursor = self._db.execute(
                """INSERT INTO memories (kind,key,content,activation,authority,source_event_id,
                   evidence_quote,importance,created_at,updated_at,expires_at)
                   VALUES (?,?,?,?,'owner',?,?,0.5,?,?,?)""",
                (
                    memory["kind"],
                    memory["key"],
                    memory["content"],
                    memory["activation"],
                    source["event_id"],
                    source["quote"],
                    now,
                    now,
                    memory["expires_at"],
                ),
            )
            memory_id = int(cursor.lastrowid)
            for old_id in dict.fromkeys([*target_ids, *hidden_ids]):
                self._db.execute(
                    "UPDATE memories SET superseded_by=?,updated_at=? WHERE id=?",
                    (memory_id, now, old_id),
                )
                self._db.execute(
                    """INSERT OR IGNORE INTO memory_evidence(memory_id,source_event_id,quote,created_at)
                       SELECT ?,source_event_id,quote,created_at FROM memory_evidence WHERE memory_id=?""",
                    (memory_id, old_id),
                )
            for citation in evidence:
                self.add_evidence(
                    memory_id, citation["event_id"], citation["quote"], now
                )
            return memory_id

    def recall_rows(self, *, now: float):
        return self._db.execute(
            """SELECT id, kind, key, content, importance, updated_at
               FROM memories
               WHERE superseded_by IS NULL
                 AND activation='recall'
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=memories.kind AND t.key=memories.key
                 )""",
            (now,),
        ).fetchall()

    def search_rows(self, *, activation=None, include_scoped=False):
        return self._db.execute(
            """SELECT id, kind, key, content, authority, evidence_quote,
                      activation, importance, updated_at,
                      (SELECT COUNT(*) FROM memory_evidence AS e
                       WHERE e.memory_id=memories.id) AS evidence_count
               FROM memories
               WHERE superseded_by IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND (? OR activation!='scoped')
                 AND (? IS NULL OR activation=?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=memories.kind AND t.key=memories.key
                 )""",
            (time.time(), include_scoped, activation, activation),
        ).fetchall()
