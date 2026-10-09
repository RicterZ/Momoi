"""Durable owner-memory requests; only private review writes effective memories."""

import json
import time

from ...models import IncomingMessage, TurnDraft
from ...memory.transactions import transaction

MEMORY_OPERATION_MAX_ATTEMPTS = 5


class MemoryOperationStore:
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
                "SELECT occurred_at FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if row:
                records.append(
                    {
                        "event_id": event_id,
                        "content": content,
                        "occurred_at": self.context_timestamp(row["occurred_at"]),
                        "occurred_at_unix": row["occurred_at"],
                    }
                )
        return records

    def next_memory_operation_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT state,retry_at FROM memory_operation_batches
               WHERE state IN ('pending','running')
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
            """SELECT id,state,retry_at FROM memory_operation_batches
               WHERE state IN ('pending','running')
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
            # `attempts` counts claims and names the attempt's Turn; the retry
            # bound reads `failures`, so an interruption cannot spend it.
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
            row = self._db.execute(
                "SELECT attempts, failures FROM memory_operation_batches "
                "WHERE id=? AND state='running'",
                (batch_id,),
            ).fetchone()
            if row is None:
                return
            # An interruption (restart or cancellation) is not a failure of the
            # batch: it returns to the queue without consuming the retry budget.
            # Only a reported error counts, and a deterministic failure (e.g. a
            # malformed request) cannot be retried into success, so the bound
            # stops it.
            failures = int(row["failures"]) + (0 if interrupted else 1)
            if not interrupted and failures >= MEMORY_OPERATION_MAX_ATTEMPTS:
                self._db.execute(
                    """UPDATE memory_operation_batches
                       SET state='failed', failures=?, error=?, updated_at=? WHERE id=?""",
                    (failures, error[:500], now, batch_id),
                )
            else:
                self._db.execute(
                    """UPDATE memory_operation_batches SET state='pending', failures=?,
                       error=?, retry_at=?, updated_at=?
                       WHERE id=? AND state='running'""",
                    (
                        failures,
                        error[:500],
                        now if interrupted else now + 300,
                        now,
                        batch_id,
                    ),
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
        with transaction(self._db):
            state = self._db.execute(
                "SELECT state FROM memory_operation_batches WHERE id=?", (batch["id"],)
            ).fetchone()
            if state is None or state["state"] != "running":
                raise ValueError("memory_operation_not_running")
            current = self.memory.validate_snapshots(snapshots)
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
                        self.memory.forget(current[memory_id], source, now=now)
                    continue
                self.memory.write(decision["memory"], target_ids, source, evidence, now=now)
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
