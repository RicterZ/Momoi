import time

from .integrity import StorageIntegrityError, decode_stored_json


class MemoryMaintenanceEvidenceStore:
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
