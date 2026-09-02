import hashlib
import json
import time

from ..models import IncomingMessage
from .turn_workflow import require_turn_workflow_kind, turn_workflow_kind_sql

EPISODE_MAINTENANCE_RESTART_REASON = (
    "process_restart_interrupted_episode_maintenance"
)


class TurnStore:
    """Turn lifecycle, usage accounting, journal, and durable tool calls."""

    def begin_turn(
        self,
        turn_id: str,
        workflow_kind: str,
        source_ids: list[str],
    ) -> str:
        workflow = require_turn_workflow_kind(workflow_kind)
        kind = "owner" if workflow == "owner" else "autonomous"
        now = time.time()
        stored_workflow = turn_workflow_kind_sql("turns")
        with self._db:
            row = self._db.execute(
                f"""SELECT state, external_effect_started, failure_reason,
                           {stored_workflow} AS stored_workflow_kind
                    FROM turns WHERE id=?""",
                (turn_id,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    """INSERT INTO turns
                       (id, kind, workflow_kind, source_ids_json, state,
                        started_at, updated_at)
                       VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                    (turn_id, kind, workflow, json.dumps(source_ids), now, now),
                )
                return "running"
            existing_workflow = row["stored_workflow_kind"]
            if existing_workflow is not None and existing_workflow != workflow:
                raise ValueError(
                    f"Turn {turn_id} belongs to {existing_workflow}, not {workflow}"
                )
            if (
                row["state"] == "cancelled"
                and row["failure_reason"] == EPISODE_MAINTENANCE_RESTART_REASON
            ):
                self._db.execute(
                    """UPDATE turns SET state='running', stage='started',
                       failure_reason=NULL, llm_calls=0, input_tokens=0,
                       output_tokens=0, started_at=?, updated_at=? WHERE id=?""",
                    (now, now, turn_id),
                )
                return "running"
            if row["state"] == "running" and row["external_effect_started"]:
                self._db.execute(
                    """UPDATE turns SET state='needs_reconciliation',
                       stage='needs_reconciliation',
                       failure_reason='process_interrupted_after_external_effect',
                       updated_at=? WHERE id=?""",
                    (now, turn_id),
                )
                self._open_reconciliation(
                    turn_id, "process_interrupted_after_external_effect", now
                )
                return "needs_reconciliation"
            if row["state"] == "running":
                self._db.execute(
                    """UPDATE turns SET stage='started', failure_reason=NULL,
                       llm_calls=0, input_tokens=0, output_tokens=0,
                       started_at=?, updated_at=? WHERE id=?""",
                    (now, now, turn_id),
                )
            return str(row["state"])

    def turn_workflow_kind(self, turn_id: str) -> str | None:
        workflow = turn_workflow_kind_sql("turns")
        row = self._db.execute(
            f"SELECT {workflow} AS workflow_kind FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None or row["workflow_kind"] is None:
            return None
        return str(row["workflow_kind"])

    def cancel_turn(
        self, turn_id: str, events: list[IncomingMessage] | None = None
    ) -> None:
        with self._db:
            row = self._db.execute(
                "SELECT external_effect_started FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self._db.execute(
                """UPDATE turns SET state='cancelled', stage='cancelled',
                   failure_reason='owner_stop', updated_at=? WHERE id=?""",
                (time.time(), turn_id),
            )
            if row is not None and row["external_effect_started"]:
                self._open_reconciliation(
                    turn_id, "owner_stopped_after_external_effect", time.time()
                )
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event.event_id,) for event in events or []),
            )

    def record_turn_failure(self, turn_id: str, reason: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE turns SET failure_reason=?, updated_at=? WHERE id=?",
                (reason[:500], time.time(), turn_id),
            )

    def complete_background_turn(self, turn_id: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (time.time(), turn_id),
            )

    def turn_has_external_effect(self, turn_id: str) -> bool:
        row = self._db.execute(
            "SELECT external_effect_started FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        return bool(row and row["external_effect_started"])

    def turn_usage(self, turn_id: str) -> dict[str, float | int]:
        row = self._db.execute(
            """SELECT started_at, llm_calls, input_tokens, output_tokens
               FROM turns WHERE id=?""",
            (turn_id,),
        ).fetchone()
        if row is None:
            return {"started_at": time.time(), "llm_calls": 0, "input": 0, "output": 0}
        return {
            "started_at": float(row["started_at"]),
            "llm_calls": int(row["llm_calls"]),
            "input": int(row["input_tokens"]),
            "output": int(row["output_tokens"]),
        }

    def record_turn_usage(
        self, turn_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET llm_calls=llm_calls+1,
                   input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                   updated_at=? WHERE id=?""",
                (
                    max(0, input_tokens),
                    max(0, output_tokens),
                    time.time(),
                    turn_id,
                ),
            )

    def append_turn_journal(
        self,
        turn_id: str,
        item_type: str,
        payload: dict[str, object],
        *,
        visibility: str = "internal",
        trust: str = "runtime",
        created_at: float | None = None,
    ) -> int:
        if visibility not in {"owner", "internal"}:
            raise ValueError("invalid journal visibility")
        if trust not in {
            "owner",
            "runtime",
            "context_data",
            "untrusted_tool_data",
        }:
            raise ValueError("invalid journal trust")
        now = time.time() if created_at is None else float(created_at)
        with self._db:
            return self._append_turn_journal(
                turn_id,
                item_type,
                payload,
                visibility=visibility,
                trust=trust,
                created_at=now,
            )

    def _append_turn_journal(
        self,
        turn_id: str,
        item_type: str,
        payload: dict[str, object],
        *,
        visibility: str,
        trust: str,
        created_at: float,
    ) -> int:
        sequence = int(
            self._db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM turn_journal WHERE turn_id=?",
                (turn_id,),
            ).fetchone()[0]
        )
        self._db.execute(
            """INSERT INTO turn_journal
               (turn_id, sequence, created_at, item_type, visibility, trust,
                payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                turn_id,
                sequence,
                created_at,
                str(item_type),
                visibility,
                trust,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            ),
        )
        return sequence

    def begin_tool_call(
        self,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        capability: str = "external_effect",
    ) -> dict[str, object] | None:
        if capability not in {"read", "write", "external_effect"}:
            raise ValueError("invalid tool capability")
        serialized = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        row = self._db.execute(
            """SELECT tool_name, arguments_sha256, state, result_json
               FROM tool_audit WHERE turn_id=? AND tool_call_id=?""",
            (turn_id, tool_call_id),
        ).fetchone()
        if row is not None:
            if row["tool_name"] != tool_name or row["arguments_sha256"] != digest:
                return {"ok": False, "error": "tool_call_id_conflict"}
            if row["state"] == "completed" and row["result_json"] is not None:
                return json.loads(str(row["result_json"]))
            return {
                "ok": False,
                "error": "previous_call_incomplete",
                "ambiguous": True,
            }
        with self._db:
            if capability != "read":
                self._db.execute(
                    """UPDATE turns SET external_effect_started=1,
                       stage='tool_dispatch', updated_at=?
                       WHERE id=? AND state='running'""",
                    (time.time(), turn_id),
                )
            else:
                self._db.execute(
                    """UPDATE turns SET stage='tool_dispatch', updated_at=?
                       WHERE id=? AND state='running'""",
                    (time.time(), turn_id),
                )
            self._db.execute(
                """INSERT INTO tool_audit
                   (turn_id, tool_call_id, tool_name, capability, arguments_sha256,
                    state, started_at)
                   VALUES (?, ?, ?, ?, ?, 'dispatching', ?)""",
                (turn_id, tool_call_id, tool_name, capability, digest, time.time()),
            )
        return None

    def complete_tool_call(
        self, turn_id: str, tool_call_id: str, result: dict[str, object]
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE tool_audit
                   SET state='completed', result_json=?, ok=?, completed_at=?
                   WHERE turn_id=? AND tool_call_id=?""",
                (
                    json.dumps(result, ensure_ascii=False),
                    int(bool(result.get("ok"))),
                    time.time(),
                    turn_id,
                    tool_call_id,
                ),
            )
            self._db.execute(
                """UPDATE turns SET stage='tool_completed', updated_at=?
                   WHERE id=? AND state='running'""",
                (time.time(), turn_id),
            )
