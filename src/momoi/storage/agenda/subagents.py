"""Background Worker tasks delegated by the Planner; one runs at a time."""
import json
import time
import uuid
from typing import Any

from ...memory.storage.transactions import transaction

SUBAGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS subagent_tasks (
 id TEXT PRIMARY KEY, source_turn_id TEXT NOT NULL, channel TEXT NOT NULL,
 title TEXT NOT NULL, task TEXT NOT NULL,
 status TEXT NOT NULL CHECK (status IN (
  'queued', 'running', 'succeeded', 'failed', 'blocked', 'cancelled')),
 turn_id TEXT, reports_json TEXT NOT NULL DEFAULT '[]',
 summary TEXT, output_refs_json TEXT NOT NULL DEFAULT '[]',
 created_at REAL NOT NULL, started_at REAL, finished_at REAL, updated_at REAL NOT NULL,
 notified_at REAL,
 UNIQUE(source_turn_id, title)
);
CREATE INDEX IF NOT EXISTS subagent_tasks_status ON subagent_tasks(status, created_at);
"""

ACTIVE_STATUSES = frozenset({"queued", "running"})
FINAL_STATUSES = frozenset({"succeeded", "failed", "blocked", "cancelled"})


class SubagentRepository:
    def __init__(self, database):
        self._db = database

    def subagent_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM subagent_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        task = dict(row)
        task["reports"] = json.loads(task.pop("reports_json") or "[]")
        task["output_refs"] = json.loads(task.pop("output_refs_json") or "[]")
        if task["status"] == "queued":
            task["queue_position"] = self._queue_position(task)
        return task

    def _queue_position(self, task: dict[str, Any]) -> int:
        row = self._db.execute(
            """SELECT COUNT(*) FROM subagent_tasks WHERE status='running'
               OR (status='queued' AND (created_at < ? OR (created_at = ? AND id < ?)))""",
            (task["created_at"], task["created_at"], task["id"]),
        ).fetchone()
        return int(row[0])

    def create_subagent_task(self, source_turn_id: str, channel: str, title: str, task: str) -> dict[str, Any]:
        title, task = title.strip(), task.strip()
        if not title or len(title) > 80:
            raise ValueError("title must contain 1 to 80 characters")
        if not task or len(task) > 6000:
            raise ValueError("task must contain 1 to 6000 characters")
        existing = self._db.execute(
            "SELECT id, task FROM subagent_tasks WHERE source_turn_id=? AND title=?",
            (source_turn_id, title),
        ).fetchone()
        if existing:
            if existing["task"] != task:
                raise ValueError("a task with this title already exists in this turn")
            return self.subagent_task(existing["id"])
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        with transaction(self._db):
            self._db.execute(
                """INSERT INTO subagent_tasks
                   (id, source_turn_id, channel, title, task, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (task_id, source_turn_id, channel, title, task, now, now),
            )
        return self.subagent_task(task_id)

    def subagent_tasks(self, channel: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """SELECT id FROM subagent_tasks WHERE channel=?
               ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END,
                        created_at DESC LIMIT ?""",
            (channel, limit),
        ).fetchall()
        return [self.subagent_task(str(row["id"])) for row in rows]

    def subagent_tasks_for_turn(self, source_turn_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id FROM subagent_tasks WHERE source_turn_id=? ORDER BY created_at", (source_turn_id,),
        ).fetchall()
        return [self.subagent_task(str(row["id"])) for row in rows]

    def claim_subagent_task(self) -> dict[str, Any] | None:
        with transaction(self._db):
            if self._db.execute("SELECT 1 FROM subagent_tasks WHERE status='running' LIMIT 1").fetchone():
                return None
            row = self._db.execute(
                "SELECT id FROM subagent_tasks WHERE status='queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            self._db.execute(
                "UPDATE subagent_tasks SET status='running', started_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
        return self.subagent_task(str(row["id"]))

    def attach_subagent_turn(self, task_id: str, turn_id: str) -> None:
        with transaction(self._db):
            self._db.execute(
                "UPDATE subagent_tasks SET turn_id=?, updated_at=? WHERE id=?",
                (turn_id, time.time(), task_id),
            )

    def add_subagent_report(self, task_id: str, progress: str) -> dict[str, Any]:
        with transaction(self._db):
            task = self.subagent_task(task_id)
            if task is None or task["status"] != "running":
                raise ValueError("task is not running")
            reports = [*task["reports"], {"at": time.time(), "progress": progress}]
            self._db.execute(
                "UPDATE subagent_tasks SET reports_json=?, updated_at=? WHERE id=?",
                (json.dumps(reports[-20:], ensure_ascii=False), time.time(), task_id),
            )
        return self.subagent_task(task_id)

    def finish_subagent_task(self, task_id: str, outcome: str, summary: str, output_refs: list[str]) -> dict[str, Any] | None:
        """Record a final outcome; returns None when the task already ended."""
        if outcome not in FINAL_STATUSES:
            raise ValueError("invalid outcome")
        now = time.time()
        with transaction(self._db):
            updated = self._db.execute(
                """UPDATE subagent_tasks SET status=?, summary=?, output_refs_json=?,
                   finished_at=?, updated_at=? WHERE id=? AND status IN ('queued','running')""",
                (outcome, summary, json.dumps(output_refs, ensure_ascii=False), now, now, task_id),
            ).rowcount
        return self.subagent_task(task_id) if updated else None

    def cancel_subagent_task(self, task_id: str, channel: str) -> dict[str, Any]:
        task = self.subagent_task(task_id)
        if task is None or task["channel"] != channel:
            raise ValueError("task not found in this channel")
        if task["status"] in FINAL_STATUSES:
            return task
        return self.finish_subagent_task(task_id, "cancelled", "派发者取消了任务。", []) or self.subagent_task(task_id)

    def cancel_subagent_tasks(self, channel: str | None = None) -> list[str]:
        rows = self._db.execute(
            """SELECT id FROM subagent_tasks WHERE status IN ('queued','running')
               AND (? IS NULL OR channel=?)""",
            (channel, channel),
        ).fetchall()
        for row in rows:
            self.finish_subagent_task(str(row["id"]), "cancelled", "用户停止了任务。", [])
        return [str(row["id"]) for row in rows]

    def mark_subagent_notified(self, task_id: str) -> None:
        with transaction(self._db):
            self._db.execute(
                "UPDATE subagent_tasks SET notified_at=? WHERE id=? AND notified_at IS NULL",
                (time.time(), task_id),
            )

    def unnotified_subagent_tasks(self, *, max_age_seconds: float) -> list[dict[str, Any]]:
        """Ended tasks whose result the Planner has not yet been woken for."""
        rows = self._db.execute(
            """SELECT id FROM subagent_tasks WHERE notified_at IS NULL
               AND status IN ('succeeded','failed','blocked') AND finished_at >= ?
               ORDER BY finished_at, id""",
            (time.time() - max_age_seconds,),
        ).fetchall()
        return [self.subagent_task(str(row["id"])) for row in rows]

    def recover_subagent_tasks(self) -> None:
        # An interrupted Worker may already have acted; never replay it.
        now = time.time()
        with transaction(self._db):
            self._db.execute(
                """UPDATE subagent_tasks SET status='failed', finished_at=?, updated_at=?,
                   summary='运行时重启，任务中断；可能已有部分操作生效。'
                   WHERE status='running'""",
                (now, now),
            )
