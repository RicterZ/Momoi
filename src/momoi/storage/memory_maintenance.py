import json
import time

from .integrity import StorageIntegrityError, decode_stored_json
from .memory import memory_snapshot_fingerprint


class MemoryMaintenanceStore:
    def queue_memory_maintenance_turn(self, turn_id: str, source_id: str) -> bool:
        now = time.time()
        with self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO turns
                   (id, kind, workflow_kind, source_ids_json, state, stage,
                    started_at, updated_at)
                   VALUES (?, 'autonomous', 'memory_maintenance', ?, 'running',
                           'memory_maintenance_queued', ?, ?)""",
                (turn_id, json.dumps([source_id]), now, now),
            )
        return cursor.rowcount == 1

    def pending_memory_maintenance_turn(self) -> str | None:
        row = self._db.execute(
            """SELECT id FROM turns
               WHERE state='running'
                 AND stage IN (
                   'memory_maintenance_queued',
                   'memory_maintenance_running'
                 )
                 AND (
                   failure_reason IS NULL
                   OR updated_at<=?
                 )
               ORDER BY started_at, id LIMIT 1""",
            (time.time() - 300,),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def recover_memory_maintenance_turns(self) -> list[str]:
        with self._db:
            self._db.execute(
                """UPDATE turns SET stage='memory_maintenance_queued',
                   failure_reason=NULL, updated_at=?
                   WHERE state='running'
                     AND stage='memory_maintenance_running'""",
                (time.time(),),
            )
            rows = self._db.execute(
                """SELECT id FROM turns
                   WHERE state='running'
                     AND stage='memory_maintenance_queued'
                   ORDER BY started_at, id"""
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def claim_memory_maintenance_turn(self, turn_id: str) -> bool:
        with self._db:
            cursor = self._db.execute(
                """UPDATE turns SET stage='memory_maintenance_running',
                   failure_reason=NULL, updated_at=?
                   WHERE id=? AND state='running'
                     AND stage='memory_maintenance_queued'""",
                (time.time(), turn_id),
            )
        return cursor.rowcount == 1

    def release_memory_maintenance_turn(self, turn_id: str, reason: str | None) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET stage='memory_maintenance_queued',
                   failure_reason=?, updated_at=?
                   WHERE id=? AND state='running'
                     AND stage='memory_maintenance_running'""",
                (
                    reason[:500] if reason is not None else None,
                    time.time(),
                    turn_id,
                ),
            )

    def memory_maintenance_source_ids(self, turn_id: str) -> list[str]:
        row = self._db.execute(
            "SELECT source_ids_json FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None:
            return []
        try:
            value = decode_stored_json(
                row["source_ids_json"],
                entity="turn",
                record_id=turn_id,
                field="source_ids_json",
                expected_type=list,
            )
        except StorageIntegrityError:
            self.record_turn_integrity_failure(turn_id, "source_ids_json")
            raise
        return [str(item) for item in value]

    def memory_maintenance_journal(self, turn_id: str) -> list[dict[str, object]]:
        rows = self._db.execute(
            """SELECT item_type, payload_json FROM turn_journal
               WHERE turn_id=? AND item_type LIKE 'memory_maintenance_%'
               ORDER BY sequence""",
            (turn_id,),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = decode_stored_json(
                    row["payload_json"],
                    entity="turn_journal",
                    record_id=turn_id,
                    field=str(row["item_type"]),
                    expected_type=dict,
                )
            except StorageIntegrityError:
                self.record_turn_integrity_failure(
                    turn_id, f"journal:{row['item_type']}"
                )
                raise
            items.append({"item_type": str(row["item_type"]), **payload})
        return items

    def record_turn_integrity_failure(self, turn_id: str, field: str) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE turns SET state='needs_reconciliation',
                   stage='needs_reconciliation', failure_reason=?, updated_at=?
                   WHERE id=? AND state='running'""",
                (f"storage_integrity_error:{field}"[:500], now, turn_id),
            )

    def latest_memory_maintenance_completion(
        self,
    ) -> dict[str, object] | None:
        rows = self._db.execute(
            """SELECT j.payload_json FROM turn_journal AS j
               JOIN turns AS t ON t.id=j.turn_id
               WHERE j.item_type='memory_maintenance_complete'
                 AND t.state='completed'
               ORDER BY j.created_at DESC, j.sequence DESC"""
        ).fetchall()
        for row in rows:
            payload = decode_stored_json(
                row["payload_json"],
                entity="turn_journal",
                record_id="memory_maintenance_complete",
                field="payload_json",
                expected_type=dict,
                fallback={},
            )
            if payload:
                return payload
        return None

    def memory_maintenance_bootstrap_complete(self) -> bool:
        rows = self._db.execute(
            """SELECT j.turn_id, j.payload_json FROM turn_journal AS j
               JOIN turns AS t ON t.id=j.turn_id
               WHERE j.item_type='memory_maintenance_complete'
                 AND t.state='completed'
               ORDER BY j.created_at DESC, j.sequence DESC"""
        ).fetchall()
        for row in rows:
            payload = decode_stored_json(
                row["payload_json"],
                entity="turn_journal",
                record_id=row["turn_id"],
                field="memory_maintenance_complete",
                expected_type=dict,
                fallback={},
            )
            if payload.get("mode") == "bootstrap":
                return True
        return False

    def latest_owner_event_marker(
        self, *, through: float | None = None
    ) -> tuple[float, str]:
        if through is None:
            row = self._db.execute(
                """SELECT received_at, id FROM events
                   ORDER BY received_at DESC, id DESC LIMIT 1"""
            ).fetchone()
        else:
            row = self._db.execute(
                """SELECT received_at, id FROM events
                   WHERE received_at<=?
                   ORDER BY received_at DESC, id DESC LIMIT 1""",
                (through,),
            ).fetchone()
        return (
            (float(row["received_at"]), str(row["id"]))
            if row is not None
            else (0.0, "")
        )

    def memory_maintenance_owner_evidence(
        self,
        *,
        after_at: float,
        after_id: str,
        through_at: float,
        through_id: str,
    ) -> list[dict[str, object]]:
        rows = self._db.execute(
            """SELECT id, content, occurred_at, received_at FROM events
               WHERE (received_at>? OR (received_at=? AND id>?))
                 AND (received_at<? OR (received_at=? AND id<=?))
               ORDER BY received_at, id""",
            (
                after_at,
                after_at,
                after_id,
                through_at,
                through_at,
                through_id,
            ),
        ).fetchall()
        return [
            {
                "event_id": str(row["id"]),
                "content": str(row["content"]),
                "occurred_at": self.context_timestamp(row["occurred_at"]),
                "received_at": float(row["received_at"]),
            }
            for row in rows
        ]

    def memory_maintenance_evidence_for_memories(
        self, memory_ids: list[int]
    ) -> list[dict[str, object]]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        rows = self._db.execute(
            f"""SELECT DISTINCT v.id, v.content, v.occurred_at, v.received_at
                FROM memory_evidence AS e
                JOIN events AS v ON v.id=e.source_event_id
                WHERE e.memory_id IN ({placeholders})
                ORDER BY v.received_at, v.id""",
            memory_ids,
        ).fetchall()
        return [
            {
                "event_id": str(row["id"]),
                "content": str(row["content"]),
                "occurred_at": self.context_timestamp(row["occurred_at"]),
                "received_at": float(row["received_at"]),
            }
            for row in rows
        ]

    def memory_maintenance_changed_ids(
        self, *, after: float, through: float
    ) -> set[int]:
        rows = self._db.execute(
            """SELECT id FROM memories AS m
               WHERE m.updated_at>? AND m.updated_at<=?
                 AND m.superseded_by IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (after, through),
        ).fetchall()
        return {int(row["id"]) for row in rows}

    def apply_memory_maintenance_batch(
        self,
        turn_id: str,
        decision: dict[str, object],
        mutable_memories: dict[int, dict[str, object]],
        *,
        owner_marker: tuple[float, str],
    ) -> None:
        now = time.time()
        with self._db:
            if self.latest_owner_event_marker() != owner_marker:
                raise ValueError("owner_evidence_changed")
            current: dict[int, dict[str, object]] = {}
            for memory_id, snapshot in mutable_memories.items():
                row = self._db.execute(
                    """SELECT id, kind, key, content, activation, authority,
                              source_event_id, evidence_quote, importance,
                              created_at, updated_at, expires_at, superseded_by
                       FROM memories AS m
                       WHERE m.id=? AND m.superseded_by IS NULL
                         AND (m.expires_at IS NULL OR m.expires_at>?)
                         AND NOT EXISTS (
                           SELECT 1 FROM memory_tombstones AS t
                           WHERE t.kind=m.kind AND t.key=m.key
                         )""",
                    (memory_id, now),
                ).fetchone()
                if row is None:
                    raise ValueError("memory_snapshot_changed")
                item = dict(row)
                if memory_snapshot_fingerprint(item) != memory_snapshot_fingerprint(
                    snapshot
                ):
                    raise ValueError("memory_snapshot_changed")
                current[memory_id] = item

            for change in decision.get("changes", []):
                if not isinstance(change, dict):
                    raise ValueError("invalid_memory_maintenance_change")
                action = str(change["action"])
                if action == "replace":
                    memory_id = int(change["memory_id"])
                    row = current[memory_id]
                    activation = str(change["activation"])
                    expires_at = change.get("expires_at")
                    if row["activation"] != "always" and activation == "always":
                        raise ValueError("memory_maintenance_promotes_always")
                    if activation == "recent":
                        if (
                            isinstance(expires_at, bool)
                            or not isinstance(expires_at, (int, float))
                            or not now < float(expires_at) <= now + 7 * 86400
                        ):
                            raise ValueError("invalid_memory_maintenance_expiry")
                    elif expires_at is not None:
                        raise ValueError("invalid_memory_maintenance_expiry")
                    evidence = change.get("evidence")
                    if isinstance(evidence, dict):
                        event_id = str(evidence["event_id"])
                        quote = str(evidence["quote"])
                        event = self._db.execute(
                            "SELECT content, occurred_at FROM events WHERE id=?",
                            (event_id,),
                        ).fetchone()
                        if event is None or quote not in str(event["content"]):
                            raise ValueError("invalid_memory_maintenance_evidence")
                        source_event_id = event_id
                        evidence_quote = quote
                        updated_at = float(event["occurred_at"])
                    else:
                        source_event_id = str(row["source_event_id"])
                        evidence_quote = str(row["evidence_quote"])
                        updated_at = float(row["updated_at"])
                    self._db.execute(
                        """UPDATE memories SET content=?, activation=?,
                           expires_at=?, source_event_id=?, evidence_quote=?,
                           updated_at=? WHERE id=? AND superseded_by IS NULL""",
                        (
                            str(change["content"]),
                            activation,
                            expires_at,
                            source_event_id,
                            evidence_quote,
                            updated_at,
                            memory_id,
                        ),
                    )
                    if isinstance(evidence, dict):
                        self._add_memory_evidence(
                            memory_id, source_event_id, evidence_quote, updated_at
                        )
                elif action == "merge":
                    survivor_id = int(change["survivor_id"])
                    source_ids = [int(item) for item in change["source_ids"]]
                    survivor = current[survivor_id]
                    activation = str(change["activation"])
                    expires_at = change.get("expires_at")
                    if survivor["activation"] != "always" and activation == "always":
                        raise ValueError("memory_maintenance_promotes_always")
                    if activation == "recent":
                        if (
                            isinstance(expires_at, bool)
                            or not isinstance(expires_at, (int, float))
                            or not now < float(expires_at) <= now + 7 * 86400
                        ):
                            raise ValueError("invalid_memory_maintenance_expiry")
                    elif expires_at is not None:
                        raise ValueError("invalid_memory_maintenance_expiry")
                    evidence_event_ids = [
                        str(event_id) for event_id in change["evidence_event_ids"]
                    ]
                    placeholders = ",".join("?" for _ in evidence_event_ids)
                    cited_events = self._db.execute(
                        f"""SELECT id,content,occurred_at FROM events
                            WHERE id IN ({placeholders})""",
                        evidence_event_ids,
                    ).fetchall()
                    if len(cited_events) != len(evidence_event_ids):
                        raise ValueError("invalid_memory_maintenance_evidence")
                    newest_event = max(
                        cited_events, key=lambda item: float(item["occurred_at"])
                    )
                    for source_id in source_ids:
                        self._db.execute(
                            """INSERT OR IGNORE INTO memory_evidence
                               (memory_id, source_event_id, quote, created_at)
                               SELECT ?, source_event_id, quote, created_at
                               FROM memory_evidence WHERE memory_id=?""",
                            (survivor_id, source_id),
                        )
                        self._db.execute(
                            """UPDATE memories SET superseded_by=?
                               WHERE id=? AND superseded_by IS NULL""",
                            (survivor_id, source_id),
                        )
                    for event in cited_events:
                        self._add_memory_evidence(
                            survivor_id,
                            str(event["id"]),
                            str(event["content"]),
                            float(event["occurred_at"]),
                        )
                    self._db.execute(
                        """UPDATE memories SET content=?, activation=?, expires_at=?,
                           source_event_id=?, evidence_quote=?, updated_at=?
                           WHERE id=? AND superseded_by IS NULL""",
                        (
                            str(change["content"]),
                            activation,
                            expires_at,
                            newest_event["id"],
                            newest_event["content"],
                            newest_event["occurred_at"],
                            survivor_id,
                        ),
                    )
                elif action == "retire":
                    memory_id = int(change["memory_id"])
                    row = current[memory_id]
                    evidence = change["evidence"]
                    assert isinstance(evidence, dict)
                    event_id = str(evidence["event_id"])
                    quote = str(evidence["quote"])
                    event = self._db.execute(
                        "SELECT content FROM events WHERE id=?", (event_id,)
                    ).fetchone()
                    if event is None or quote not in str(event["content"]):
                        raise ValueError("invalid_memory_maintenance_evidence")
                    sibling = self._db.execute(
                        """SELECT 1 FROM memories
                           WHERE kind=? AND key=? AND id<>?
                             AND superseded_by IS NULL LIMIT 1""",
                        (row["kind"], row["key"], memory_id),
                    ).fetchone()
                    if sibling is not None:
                        raise ValueError("memory_maintenance_tombstone_conflict")
                    self._db.execute(
                        """INSERT INTO memory_tombstones
                           (kind, key, source_event_id, evidence_quote, created_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(kind,key) DO UPDATE SET
                             source_event_id=excluded.source_event_id,
                             evidence_quote=excluded.evidence_quote,
                             created_at=excluded.created_at""",
                        (row["kind"], row["key"], event_id, quote, now),
                    )
                else:
                    raise ValueError("invalid_memory_maintenance_change")

            self._append_turn_journal(
                turn_id,
                "memory_maintenance_batch",
                {
                    "reviewed_ids": list(decision.get("reviewed_ids", [])),
                    "completed_ids": list(decision.get("completed_ids", [])),
                    "change_count": len(decision.get("changes", [])),
                    "regroup_requests": list(decision.get("regroup_requests", [])),
                    "summary": str(decision.get("summary") or ""),
                    "owner_marker": [owner_marker[0], owner_marker[1]],
                },
                visibility="internal",
                trust="runtime",
                created_at=now,
            )
