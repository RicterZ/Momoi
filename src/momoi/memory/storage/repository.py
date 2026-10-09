from __future__ import annotations

import json
import sqlite3
import time
from typing import cast

from ..metadata import MemoryFilters, TagCatalog
from .records import ActiveMemory, InventoryMemory, MEMORY_ACTIVATIONS, memory_snapshot_fingerprint, legacy_scope
from .transactions import transaction


class MemoryRepository:
    """Uses a caller-owned connection with sqlite3.Row and an existing schema.

    Mutations compose through savepoints inside an existing transaction, or
    commit their own transaction when used alone. Inputs are validated commands;
    authentication and source-text verification belong to the caller. Connection
    lifetime and schema migrations remain the caller's responsibility.
    """
    def __init__(self, database: sqlite3.Connection, *, tags: TagCatalog | None = None) -> None:
        self._db = database
        self.tags = tags or TagCatalog()

    @staticmethod
    def _record(row):
        record = dict(row)
        meta = json.loads(record.pop("meta_json"))
        # Reading does not reclassify historical tags after catalog changes.
        record["meta"] = {"tags": meta.get("tags", [])}
        return record

    def _filter_sql(self, filters: MemoryFilters | None):
        filters = self.tags.filters(filters)
        clauses, params = [], []
        if filters["kinds"]:
            clauses.append("kind IN (" + ",".join("?" for _ in filters["kinds"]) + ")")
            params.extend(filters["kinds"])
        if filters["tags_any"]:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(meta_json, '$.tags') "
                "WHERE value IN (" + ",".join("?" for _ in filters["tags_any"]) + "))"
            )
            params.extend(filters["tags_any"])
        return "".join(" AND " + clause for clause in clauses), params

    def update_meta(self, snapshot, meta, *, now: float | None = None) -> None:
        """Apply a reviewed tag edit; reject stale snapshots and preserve vectors."""
        meta = self.tags.validate(meta)
        with transaction(self._db):
            current = self.validate_snapshots({int(snapshot["id"]): snapshot})[int(snapshot["id"])]
            if meta == current["meta"]:
                return
            self._db.execute(
                "UPDATE memories SET meta_json=?, updated_at=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False, sort_keys=True),
                 time.time() if now is None else now, snapshot["id"]),
            )

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

    def replace(self, memory_id, content, activation, expires_at, source, *, updated_at) -> int:
        """Create a replacement version with the same identity and inherited evidence."""
        with transaction(self._db):
            row = self.snapshots([memory_id]).get(memory_id)
            if row is None:
                raise ValueError("memory_snapshot_changed")
            return self.write(
                {"kind": row["kind"], "key": row["key"], "content": content,
                 "activation": activation, "expires_at": expires_at},
                [memory_id], source, [source], now=updated_at,
            )

    def merge(self, survivor_id, source_ids, content, activation, expires_at, events) -> int:
        """Use the selected identity for a new version, preserving all source evidence."""
        newest = max(events, key=lambda item: float(item["occurred_at"]))
        with transaction(self._db):
            targets = [survivor_id, *source_ids]
            current = self.snapshots(targets)
            if len(set(targets)) != len(targets) or set(current) != set(targets):
                raise ValueError("memory_snapshot_changed")
            if len({legacy_scope(row) for row in current.values()}) != 1:
                raise ValueError("memory merge cannot cross scopes")
            survivor = current[survivor_id]
            return self.write(
                {"kind": survivor["kind"], "key": survivor["key"], "content": content,
                 "activation": activation, "expires_at": expires_at}, targets,
                {"event_id": newest["id"], "quote": newest["content"]},
                [{"event_id": event["id"], "quote": event["content"]} for event in events],
                now=float(newest["occurred_at"]),
            )

    def inventory(self) -> list[InventoryMemory]:
        self.purge_expired()
        now = time.time()
        rows = self._db.execute(
            """SELECT id, kind, key, content, activation, authority,
                      source_event_id, evidence_quote, importance,
                      created_at, updated_at, expires_at, superseded_by, meta_json
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
        return [cast(InventoryMemory, self._record(row)) for row in rows]

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
            """SELECT id, kind, key, content, activation, importance, updated_at, meta_json
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
        return [self._record(row) for row in rows]

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
            """SELECT id, kind, key, content, importance, meta_json FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.id DESC LIMIT 1""",
            (kind, key, time.time()),
        ).fetchone()
        return cast(ActiveMemory, self._record(row)) if row else None

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
        return {int(row["id"]): self._record(row) for row in rows}


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
            meta = memory.get("meta")
            if "meta" not in memory:
                previous = self.snapshots(list(target_ids))
                meta = {"tags": sorted({tag for row in previous.values() for tag in row["meta"]["tags"]})}
            meta = self.tags.validate(meta)
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
                   evidence_quote,importance,created_at,updated_at,expires_at,meta_json)
                   VALUES (?,?,?,?,'owner',?,?,0.5,?,?,?,?)""",
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
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
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
                self._db.execute(
                    """INSERT OR IGNORE INTO memory_evidence(memory_id,source_event_id,quote,created_at)
                       SELECT ?,source_event_id,evidence_quote,created_at FROM memories WHERE id=?""",
                    (memory_id, old_id),
                )
            for citation in evidence:
                self.add_evidence(
                    memory_id, citation["event_id"], citation["quote"], now
                )
            return memory_id

    def planning_rows(self, ids=None):
        """Current versions, including forgotten records, for private write review."""
        params = [time.time()]
        clause = ""
        if ids is not None:
            if not ids:
                return []
            clause = " AND m.id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        rows = self._db.execute(
            """SELECT m.*, t.source_event_id AS forgotten_event_id,
                      t.evidence_quote AS forgotten_quote, t.created_at AS forgotten_at
               FROM memories m LEFT JOIN memory_tombstones t ON t.kind=m.kind AND t.key=m.key
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at>? OR t.kind IS NOT NULL)"""
            + clause + " ORDER BY m.id", params,
        ).fetchall()
        return [self._record(row) for row in rows]

    def tombstone(self, kind, key):
        row = self._db.execute(
            "SELECT * FROM memory_tombstones WHERE kind=? AND key=?", (kind, key),
        ).fetchone()
        return dict(row) if row else None

    def validate_forgotten(self, snapshots):
        current = {row["id"]: row for row in self.planning_rows(list(snapshots))
                   if row["forgotten_at"] is not None}
        if current != snapshots:
            raise ValueError("memory_forgotten_snapshot_changed")

    def recall_rows(self, *, now: float, filters: MemoryFilters | None = None):
        clause, params = self._filter_sql(filters)
        rows = self._db.execute(
            f"""SELECT id, kind, key, content, importance, updated_at, meta_json
               FROM memories
               WHERE superseded_by IS NULL
                 AND activation='recall'
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=memories.kind AND t.key=memories.key
                 ){clause}""",
            (now, *params),
        ).fetchall()
        return [self._record(row) for row in rows]

    def search_rows(self, *, activation=None, include_scoped=False, filters=None):
        clause, params = self._filter_sql(filters)
        rows = self._db.execute(
            f"""SELECT id, kind, key, content, authority, evidence_quote,
                      activation, importance, updated_at, meta_json,
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
                 ){clause}""",
            (time.time(), include_scoped, activation, activation, *params),
        ).fetchall()
        return [self._record(row) for row in rows]

    def recall_row(self, source_id: str):
        row = self._db.execute(
            """SELECT id, kind, key, content, meta_json FROM memories AS m
               WHERE id=? AND superseded_by IS NULL AND activation='recall'
                 AND (expires_at IS NULL OR expires_at>?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (source_id, time.time()),
        ).fetchone()
        return self._record(row) if row is not None else None
