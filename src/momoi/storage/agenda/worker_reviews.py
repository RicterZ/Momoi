"""Offline review of finished Worker tasks; candidates are not memories yet."""
import json
import time
from typing import Any

from ...memory.storage.transactions import transaction

WORKER_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_reviews (
 task_id TEXT PRIMARY KEY, local_date TEXT NOT NULL, admitted INTEGER NOT NULL,
 reasons_json TEXT NOT NULL DEFAULT '[]', turn_id TEXT, reviewed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_experience_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT, local_date TEXT NOT NULL, turn_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('execution', 'dispatch')),
 problem TEXT NOT NULL, applies_to TEXT NOT NULL, content TEXT NOT NULL,
 pitfalls_json TEXT NOT NULL DEFAULT '[]', evidence_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'candidate' CHECK (state IN ('candidate', 'deleted')),
 created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS worker_experience_candidates_date
 ON worker_experience_candidates(local_date, id);
"""

FINAL_TASK_STATUSES = ("succeeded", "failed", "blocked", "cancelled")
WORKFLOW_TOOLS = frozenset({"subagent_finish", "subagent_report"})


class WorkerReviewStore:
    def unreviewed_worker_tasks(self, *, start_at: float, end_at: float) -> list[dict[str, Any]]:
        """Finished tasks in the window that no review has evaluated yet."""
        rows = self._db.execute(
            f"""SELECT t.* FROM subagent_tasks t
                WHERE t.status IN ({','.join('?' for _ in FINAL_TASK_STATUSES)})
                  AND t.finished_at >= ? AND t.finished_at < ? AND t.turn_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM worker_reviews r WHERE r.task_id=t.id)
                ORDER BY t.finished_at, t.id""",
            (*FINAL_TASK_STATUSES, start_at, end_at),
        ).fetchall()
        return [dict(row) for row in rows]

    def worker_task_steps(self, turn_id: str) -> list[dict[str, Any]]:
        """Tool calls of one Worker turn paired with their results, in call order."""
        steps: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for row in self._db.execute(
            "SELECT item_type, payload_json FROM turn_journal WHERE turn_id=? "
            "AND item_type IN ('tool_call', 'tool_result') ORDER BY sequence",
            (turn_id,),
        ):
            payload = json.loads(row["payload_json"])
            call_id = str(payload.get("tool_call_id") or "")
            if payload.get("name") in WORKFLOW_TOOLS:
                continue
            if row["item_type"] == "tool_call":
                step = {"name": str(payload.get("name")), "arguments": payload.get("arguments"),
                        "ok": None, "error": None, "result": None}
                by_id[call_id] = step
                steps.append(step)
            elif call_id in by_id:
                by_id[call_id].update(ok=bool(payload.get("ok")), error=payload.get("error"),
                                      result=payload.get("result"))
        return steps

    def commit_worker_review(
        self,
        local_date: str,
        turn_id: str | None,
        decisions: list[dict[str, Any]],
        experiences: list[dict[str, Any]],
    ) -> None:
        now = time.time()
        with transaction(self._db):
            for decision in decisions:
                self._db.execute(
                    "INSERT OR IGNORE INTO worker_reviews "
                    "(task_id, local_date, admitted, reasons_json, turn_id, reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (decision["task_id"], local_date, int(bool(decision["reasons"])),
                     json.dumps(decision["reasons"], ensure_ascii=False), turn_id, now),
                )
            for item in experiences:
                self._db.execute(
                    "INSERT INTO worker_experience_candidates "
                    "(local_date, turn_id, kind, problem, applies_to, content, pitfalls_json, evidence_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (local_date, turn_id, item["kind"], item["problem"], item["applies_to"], item["content"],
                     json.dumps(item.get("pitfalls", []), ensure_ascii=False),
                     json.dumps(item["evidence"], ensure_ascii=False), now),
                )

    def worker_experience_candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM worker_experience_candidates WHERE state='candidate' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{**dict(row), "pitfalls": json.loads(row["pitfalls_json"]),
                 "evidence": json.loads(row["evidence_json"])} for row in rows]
