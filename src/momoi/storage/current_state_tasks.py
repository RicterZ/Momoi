"""Durable state maintenance input, activated by the source turn's commit."""

import json
import time

from .current_state_contract import CURRENT_STATE_SOURCE_STAGES


class CurrentStateTaskStore:
    def stage_current_state_task(
        self, source_turn_id, source_stage, system, messages, input_index, tools
    ):
        if source_stage not in CURRENT_STATE_SOURCE_STAGES:
            raise ValueError("current_state_source_not_allowed")
        row = self._db.execute(
            "SELECT workflow_kind,state FROM turns WHERE id=?", (source_turn_id,)
        ).fetchone()
        if row is None or tuple(row) != (source_stage, "running"):
            return False
        payload = json.dumps(
            {
                "system": system,
                "messages": messages,
                "input_index": input_index,
                "tools": tools,
            },
            ensure_ascii=False,
        )
        with self._db:
            self._db.execute(
                """INSERT INTO current_state_tasks(source_turn_id,source_stage,state,payload_json,created_at)
                   VALUES (?,?,'staged',?,?) ON CONFLICT(source_turn_id) DO UPDATE
                   SET payload_json=excluded.payload_json,created_at=excluded.created_at
                   WHERE current_state_tasks.state='staged'""",
                (source_turn_id, source_stage, payload, time.time()),
            )
        return True

    def pending_current_state_task(self):
        row = self._db.execute(
            """SELECT q.source_turn_id,q.state,q.retry_at FROM current_state_tasks q
               JOIN turns t ON t.id=q.source_turn_id
               WHERE q.state IN ('pending','running') AND t.state='completed' AND t.failure_reason IS NULL
               ORDER BY q.committed_at,q.rowid LIMIT 1"""
        ).fetchone()
        if row and row["state"] == "pending" and row["retry_at"] <= time.time():
            return str(row["source_turn_id"])
        return None

    def recover_current_state_tasks(self):
        with self._db:
            self._db.execute(
                """UPDATE turns SET state='cancelled',stage='cancelled',failure_reason='process_restart'
                   WHERE workflow_kind='current_state_maintenance' AND state='running'"""
            )
            self._db.execute(
                "UPDATE current_state_tasks SET state='pending',retry_at=0 WHERE state='running'"
            )
            self._db.execute("DELETE FROM current_state_tasks WHERE state='staged'")

    def claim_current_state_task(self, source_turn_id):
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self.pending_current_state_task() != source_turn_id:
                return None
            row = self._db.execute(
                "SELECT * FROM current_state_tasks WHERE source_turn_id=?",
                (source_turn_id,),
            ).fetchone()
            if row is None:
                return None
            applied = self._db.execute(
                "SELECT 1 FROM current_state_changes WHERE operation_id=?",
                (f"current-state:{source_turn_id}",),
            ).fetchone()
            if applied:
                self._db.execute(
                    "UPDATE current_state_tasks SET state='completed',payload_json=NULL,error=NULL WHERE source_turn_id=?",
                    (source_turn_id,),
                )
                if row["maintenance_turn_id"]:
                    self._db.execute(
                        "UPDATE turns SET state='completed',stage='completed',failure_reason=NULL,updated_at=? WHERE id=?",
                        (time.time(), row["maintenance_turn_id"]),
                    )
                return None
            payload = json.loads(row["payload_json"])
            attempt = int(row["attempts"]) + 1
            turn_id = f"current-state:{source_turn_id}:{attempt}"
            now = time.time()
            self._db.execute(
                """INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at)
                   VALUES (?,'autonomous','current_state_maintenance',?,'running',?,?)""",
                (turn_id, json.dumps([source_turn_id]), now, now),
            )
            self._db.execute(
                "UPDATE current_state_tasks SET state='running',attempts=?,maintenance_turn_id=?,error=NULL WHERE source_turn_id=?",
                (attempt, turn_id, source_turn_id),
            )
        return {**dict(row), **payload, "turn_id": turn_id, "attempts": attempt}

    def current_state_task_is_current(self, source_turn_id, turn_id):
        return (
            self._db.execute(
                """SELECT 1 FROM current_state_tasks q JOIN turns t ON t.id=q.source_turn_id
               WHERE q.source_turn_id=? AND q.maintenance_turn_id=? AND q.state='running'
                 AND t.state='completed' AND t.failure_reason IS NULL""",
                (source_turn_id, turn_id),
            ).fetchone()
            is not None
        )

    def finish_current_state_task(self, source_turn_id, turn_id):
        with self._db:
            self._db.execute(
                """UPDATE current_state_tasks SET state='completed',payload_json=NULL,error=NULL
                   WHERE source_turn_id=? AND maintenance_turn_id=? AND state='running'""",
                (source_turn_id, turn_id),
            )
            self._db.execute(
                "UPDATE turns SET state='completed',stage='completed',updated_at=? WHERE id=?",
                (time.time(), turn_id),
            )

    def release_current_state_task(
        self, source_turn_id, turn_id, error, *, interrupted=False
    ):
        with self._db:
            self._db.execute(
                """UPDATE current_state_tasks SET state='pending',retry_at=?,error=?
                   WHERE source_turn_id=? AND maintenance_turn_id=? AND state='running'""",
                (
                    time.time() + (0 if interrupted else 60),
                    error,
                    source_turn_id,
                    turn_id,
                ),
            )
            self._db.execute(
                "UPDATE turns SET state='cancelled',stage='cancelled',failure_reason=?,updated_at=? WHERE id=? AND state='running'",
                (error, time.time(), turn_id),
            )
