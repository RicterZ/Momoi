"""Durable state maintenance input, activated by the source turn's commit."""

import json
import time

from .current_state_contract import CURRENT_STATE_SOURCE_STAGES

# Trigger threshold; a claim includes every eligible pending Turn.
CURRENT_STATE_BATCH_SIZE = 6
CURRENT_STATE_FULL_IDLE_SECONDS = 60
CURRENT_STATE_PARTIAL_IDLE_SECONDS = 300


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

    def current_state_batch_status(self):
        rows = self._db.execute(
            """SELECT q.source_turn_id,q.state,q.retry_at,q.committed_at
               FROM current_state_tasks q
               JOIN turns t ON t.id=q.source_turn_id
               WHERE q.state IN ('pending','running')
                 AND t.state='completed' AND t.failure_reason IS NULL
               ORDER BY q.committed_at,q.rowid"""
        ).fetchall()
        if not rows or rows[0]["state"] != "pending":
            return None
        return {
            "source_turn_id": str(rows[0]["source_turn_id"]),
            "count": len(rows),
            "retry_at": float(rows[0]["retry_at"]),
            "latest_committed_at": max(float(row["committed_at"] or 0) for row in rows),
        }

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

    def claim_current_state_batch(self, source_turn_id):
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            while True:
                status = self.current_state_batch_status()
                if status is None or status["source_turn_id"] != source_turn_id:
                    return None
                head = self._db.execute(
                    "SELECT * FROM current_state_tasks WHERE source_turn_id=?",
                    (source_turn_id,),
                ).fetchone()
                previous_turn_id = str(head["maintenance_turn_id"] or "")
                applied = previous_turn_id and self._db.execute(
                    "SELECT 1 FROM current_state_changes WHERE operation_id=?",
                    (f"current-state:{previous_turn_id}",),
                ).fetchone()
                if not applied:
                    break
                self._db.execute(
                    """UPDATE current_state_tasks
                       SET state='completed',payload_json=NULL,error=NULL
                       WHERE maintenance_turn_id=?""",
                    (previous_turn_id,),
                )
                self._db.execute(
                    """UPDATE turns SET state='completed',stage='completed',
                       failure_reason=NULL,updated_at=? WHERE id=?""",
                    (time.time(), previous_turn_id),
                )
            if status["retry_at"] > time.time():
                return None
            rows = self._db.execute(
                """SELECT q.* FROM current_state_tasks q
                   JOIN turns t ON t.id=q.source_turn_id
                   WHERE q.state='pending' AND q.retry_at<=?
                     AND t.state='completed' AND t.failure_reason IS NULL
                   ORDER BY q.committed_at,q.rowid""",
                (time.time(),),
            ).fetchall()
            if not rows or str(rows[0]["source_turn_id"]) != source_turn_id:
                return None
            attempt = int(rows[0]["attempts"]) + 1
            turn_id = f"current-state:{source_turn_id}:{attempt}"
            now = time.time()
            source_turn_ids = [str(row["source_turn_id"]) for row in rows]
            self._db.execute(
                """INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at)
                   VALUES (?,'autonomous','current_state_maintenance',?,'running',?,?)""",
                (turn_id, json.dumps(source_turn_ids), now, now),
            )
            self._db.executemany(
                """UPDATE current_state_tasks
                   SET state='running',attempts=attempts+1,
                       maintenance_turn_id=?,error=NULL
                   WHERE source_turn_id=?""",
                ((turn_id, value) for value in source_turn_ids),
            )
        tasks = [
            {**dict(row), **json.loads(row["payload_json"])} for row in rows
        ]
        return {
            "source_turn_id": source_turn_ids[-1],
            "source_turn_ids": source_turn_ids,
            "turn_id": turn_id,
            "attempts": attempt,
            "tasks": tasks,
        }

    def current_state_batch_is_current(self, source_turn_ids, turn_id):
        placeholders = ",".join("?" for _ in source_turn_ids)
        count = self._db.execute(
            f"""SELECT COUNT(*) FROM current_state_tasks q
                JOIN turns t ON t.id=q.source_turn_id
                WHERE q.source_turn_id IN ({placeholders})
                  AND q.maintenance_turn_id=? AND q.state='running'
                  AND t.state='completed' AND t.failure_reason IS NULL""",
            (*source_turn_ids, turn_id),
        ).fetchone()[0]
        return count == len(source_turn_ids)

    def finish_current_state_batch(self, source_turn_ids, turn_id):
        placeholders = ",".join("?" for _ in source_turn_ids)
        with self._db:
            self._db.execute(
                f"""UPDATE current_state_tasks
                    SET state='completed',payload_json=NULL,error=NULL
                    WHERE source_turn_id IN ({placeholders})
                      AND maintenance_turn_id=? AND state='running'""",
                (*source_turn_ids, turn_id),
            )
            self._db.execute(
                "UPDATE turns SET state='completed',stage='completed',updated_at=? WHERE id=?",
                (time.time(), turn_id),
            )

    def release_current_state_batch(
        self, source_turn_ids, turn_id, error, *, interrupted=False
    ):
        placeholders = ",".join("?" for _ in source_turn_ids)
        with self._db:
            self._db.execute(
                f"""UPDATE current_state_tasks SET state='pending',retry_at=?,error=?
                    WHERE source_turn_id IN ({placeholders})
                      AND maintenance_turn_id=? AND state='running'""",
                (
                    time.time() + (0 if interrupted else 60),
                    error,
                    *source_turn_ids,
                    turn_id,
                ),
            )
            self._db.execute(
                "UPDATE turns SET state='cancelled',stage='cancelled',failure_reason=?,updated_at=? WHERE id=? AND state='running'",
                (error, time.time(), turn_id),
            )
