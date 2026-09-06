"""Durable owner-memory requests; only private review writes effective memories."""

import json
import time

from ..models import IncomingMessage, TurnDraft
from .memory_values import memory_snapshot_fingerprint


class MemoryOperationStore:
    def memory_snapshots(self, ids: list[int]) -> dict[int, dict[str, object]]:
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

    def injected_memory_snapshots(self) -> dict[int, dict[str, object]]:
        self.purge_expired_memories()
        ids = [
            int(row["id"])
            for activation in ("always", "recent")
            for row in self._memory_rows(activation)
        ]
        return self.memory_snapshots(ids)

    def _queue_memory_operations(
        self,
        source_turn_id: str,
        draft: TurnDraft | None,
        events: list[IncomingMessage],
        now: float,
    ) -> None:
        if draft is None or not draft.memory_operations:
            return
        evidence = {event.event_id: event for event in events}
        for operation in draft.memory_operations:
            event = evidence.get(operation["event_id"])
            if event is None or operation["evidence"] not in event.text:
                raise ValueError("memory_operation_evidence_changed")
        self._db.execute(
            """INSERT OR IGNORE INTO memory_operation_batches
               (id, operations_json, context_json, conversation_json, events_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                source_turn_id,
                json.dumps(draft.memory_operations, ensure_ascii=False),
                json.dumps(list(draft.memory_context.values()), ensure_ascii=False),
                json.dumps(draft.memory_conversation, ensure_ascii=False),
                json.dumps([vars(event) for event in events], ensure_ascii=False),
                now,
                now,
            ),
        )

    def memory_operation_evidence_records(
        self, evidence: dict[str, str]
    ) -> list[dict[str, object]]:
        records = []
        for event_id, content in evidence.items():
            row = self._db.execute(
                "SELECT occurred_at,received_at FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if row:
                records.append(
                    {
                        "event_id": event_id,
                        "content": content,
                        "occurred_at": self.context_timestamp(row["occurred_at"]),
                        "occurred_at_unix": row["occurred_at"],
                        "received_at": row["received_at"],
                    }
                )
        return records

    def next_memory_operation_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT state,retry_at FROM memory_operation_batches WHERE state<>'completed'
               ORDER BY sequence LIMIT 1"""
        ).fetchone()
        # Ready work is already enqueued; only a future retry needs a timer.
        return (
            float(row["retry_at"])
            if row and row["state"] == "pending" and row["retry_at"] > time.time()
            else None
        )

    def pending_memory_operation(self) -> str | None:
        row = self._db.execute(
            """SELECT id,state,retry_at FROM memory_operation_batches WHERE state<>'completed'
               ORDER BY sequence LIMIT 1"""
        ).fetchone()
        return (
            str(row["id"])
            if row and row["state"] == "pending" and row["retry_at"] <= time.time()
            else None
        )

    def recover_memory_operations(self) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE turns SET state='cancelled', stage='cancelled',
                   failure_reason='process_restart', updated_at=?
                   WHERE workflow_kind='memory_operation' AND state='running'""",
                (now,),
            )
            self._db.execute(
                "UPDATE memory_operation_batches SET state='pending', retry_at=0 WHERE state='running'"
            )

    def claim_memory_operation(self, batch_id: str) -> dict[str, object] | None:
        now = time.time()
        with self._db:
            if self.pending_memory_operation() != batch_id:
                return None
            cursor = self._db.execute(
                """UPDATE memory_operation_batches SET state='running', attempts=attempts+1,
                   error=NULL, updated_at=? WHERE id=? AND state='pending' AND retry_at<=?""",
                (now, batch_id, now),
            )
            if cursor.rowcount != 1:
                return None
            row = dict(
                self._db.execute(
                    "SELECT * FROM memory_operation_batches WHERE id=?", (batch_id,)
                ).fetchone()
            )
            turn_id = f"memory-operation:{batch_id}:{row['attempts']}"
            self._db.execute(
                """INSERT INTO turns (id,kind,workflow_kind,source_ids_json,state,started_at,updated_at)
                   VALUES (?,'autonomous','memory_operation',?,'running',?,?)""",
                (turn_id, json.dumps([batch_id]), now, now),
            )
        return {
            **row,
            "turn_id": turn_id,
            **{
                key: json.loads(row[key + "_json"])
                for key in ("operations", "context", "conversation", "events")
            },
        }

    def release_memory_operation(
        self, batch_id: str, turn_id: str, error: str, *, interrupted: bool = False
    ) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE memory_operation_batches SET state='pending', error=?, retry_at=?, updated_at=?
                   WHERE id=? AND state='running'""",
                (error[:500], now if interrupted else now + 300, now, batch_id),
            )
            self._db.execute(
                """UPDATE turns SET state='cancelled',stage='cancelled',failure_reason=?,updated_at=?
                   WHERE id=? AND state='running'""",
                (error[:500], now, turn_id),
            )

    def apply_memory_operation(
        self,
        batch: dict[str, object],
        decisions: list[dict[str, object]],
        snapshots: dict[int, dict[str, object]],
    ) -> None:
        now = time.time()
        with self._db:
            state = self._db.execute(
                "SELECT state FROM memory_operation_batches WHERE id=?", (batch["id"],)
            ).fetchone()
            if state is None or state["state"] != "running":
                raise ValueError("memory_operation_not_running")
            current = self.memory_snapshots(list(snapshots))
            if set(current) != set(snapshots) or any(
                memory_snapshot_fingerprint(current[key])
                != memory_snapshot_fingerprint(snapshot)
                for key, snapshot in snapshots.items()
            ):
                raise ValueError("memory_snapshot_changed")
            for decision in decisions:
                action = decision["action"]
                if action in {"noop", "defer"}:
                    continue
                evidence = decision["evidence"]
                for citation in evidence:
                    row = self._db.execute(
                        "SELECT content FROM events WHERE id=?", (citation["event_id"],)
                    ).fetchone()
                    if row is None or citation["quote"] not in row["content"]:
                        raise ValueError("memory_operation_evidence_changed")
                last_request = next(
                    item
                    for item in reversed(batch["operations"])
                    if item["id"] in decision["operation_ids"]
                )
                source = next(
                    item
                    for item in evidence
                    if item["event_id"] == last_request["event_id"]
                )
                target_ids = decision["target_ids"]
                if action == "forget":
                    for memory_id in target_ids:
                        memory = current[memory_id]
                        self._db.execute(
                            """INSERT INTO memory_tombstones(kind,key,source_event_id,evidence_quote,created_at)
                               VALUES (?,?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET
                               source_event_id=excluded.source_event_id,evidence_quote=excluded.evidence_quote,
                               created_at=excluded.created_at""",
                            (
                                memory["kind"],
                                memory["key"],
                                source["event_id"],
                                source["quote"],
                                now,
                            ),
                        )
                    continue
                memory = decision["memory"]
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
                    self._add_memory_evidence(
                        memory_id, citation["event_id"], citation["quote"], now
                    )
            self._db.execute(
                """UPDATE memory_operation_batches SET state='completed',result_json=?,error=NULL,updated_at=? WHERE id=?""",
                (json.dumps(decisions, ensure_ascii=False), now, batch["id"]),
            )
            self._append_turn_journal(
                str(batch["turn_id"]),
                "memory_operation_result",
                {"decisions": decisions},
                visibility="internal",
                trust="runtime",
                created_at=now,
            )
            self._db.execute(
                "UPDATE turns SET state='completed',stage='completed',updated_at=? WHERE id=?",
                (now, batch["turn_id"]),
            )
