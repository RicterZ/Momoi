import json
import time


class MemoryMaintenanceQueueStore:
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
