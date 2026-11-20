"""Offline review of finished Worker tasks; findings join the reflection candidates."""
import json
import time
from typing import Any

from ...memory.storage.transactions import transaction

WORKER_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_reviews (
 task_id TEXT PRIMARY KEY, local_date TEXT NOT NULL, admitted INTEGER NOT NULL,
 reasons_json TEXT NOT NULL DEFAULT '[]', turn_id TEXT, reviewed_at REAL NOT NULL
);
"""

FINAL_TASK_STATUSES = ("succeeded", "failed", "blocked", "cancelled")
WORKFLOW_TOOLS = frozenset({"subagent_finish", "subagent_report", "subagent_block"})


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
        practices: list[dict[str, Any]],
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
            for practice in practices:
                self.add_worker_practice_candidate(local_date, practice, now)

    def add_worker_practice_candidate(self, local_date: str, practice: dict[str, Any], now: float) -> None:
        """Merge one practice into reflection candidates; each Worker task counts once."""
        from ..reflection.candidates import current_events

        old = self._db.execute("SELECT * FROM reflection_candidates WHERE key=?", (practice["key"],)).fetchone()
        if old and (old["state"] != "active" or old["kind"] != "practice"
                    or self._reflection_candidate_review(old) in ("pending", "running")):
            return
        events = current_events(json.loads(old["events_json"]), local_date) if old else []
        known = {source for event in events for source in event["sources"]}
        events += [event for event in practice["events"] if not known.intersection(event["sources"])]
        edited = bool(old and old["edited"])
        content = old["content"] if edited else practice["content"]
        triggers = old["triggers_json"] if edited else json.dumps(practice["triggers"], ensure_ascii=False)
        self._db.execute(
            """INSERT INTO reflection_candidates(key,kind,content,events_json,conflicts_json,updated_at,triggers_json)
               VALUES (?,'practice',?,?,'[]',?,?) ON CONFLICT(key) DO UPDATE SET
               kind=excluded.kind,content=excluded.content,triggers_json=excluded.triggers_json,
               events_json=excluded.events_json,updated_at=excluded.updated_at,revision=revision+1""",
            (practice["key"], content, json.dumps(events, ensure_ascii=False), now, triggers),
        )

    def worker_practice_candidates(self) -> list[dict[str, Any]]:
        """Active practice candidates, so a review can extend one instead of duplicating it."""
        rows = self._db.execute(
            "SELECT key, content, events_json FROM reflection_candidates WHERE state='active' AND kind='practice' "
            "ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        return [{"key": row["key"], "content": row["content"], "count": len(json.loads(row["events_json"]))}
                for row in rows]
