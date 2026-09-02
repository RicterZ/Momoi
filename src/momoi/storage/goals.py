import json
import sqlite3
import time

from ..context_time import context_timestamp
from ..models import TurnDraft
from .scheduling import next_schedule_at, normalize_schedule


class GoalStore:
    """Goal persistence, scheduling, claims, and Turn-draft mutations."""

    def _normalize_goal_schedules(self, now: float) -> None:
        """Remove the retired per-Goal timezone and reschedule in app time."""
        rows = self._db.execute(
            """SELECT id, status, schedule_json, next_review_at
               FROM goals WHERE schedule_json<>''"""
        ).fetchall()
        for row in rows:
            schedule = json.loads(str(row["schedule_json"]))
            if not isinstance(schedule, dict):
                raise ValueError(f"goal {row['id']} has an invalid schedule")
            schedule.pop("timezone", None)
            normalized = normalize_schedule(schedule)
            encoded = json.dumps(
                normalized, ensure_ascii=False, separators=(",", ":")
            )
            if (
                normalized["kind"] == "daily"
                and row["status"] in {"active", "waiting"}
                and float(row["next_review_at"] or 0) > now
            ):
                self._db.execute(
                    "UPDATE goals SET schedule_json=?, next_review_at=? WHERE id=?",
                    (
                        encoded,
                        next_schedule_at(normalized, self._timezone, now),
                        row["id"],
                    ),
                )
            else:
                self._db.execute(
                    "UPDATE goals SET schedule_json=? WHERE id=?",
                    (encoded, row["id"]),
                )

    def goal(self, goal_id: str) -> dict[str, object] | None:
        row = self._db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return self._goal_dict(row) if row else None

    def list_goals(self, include_closed: bool = False) -> list[dict[str, object]]:
        where = (
            "" if include_closed else "WHERE status IN ('active', 'waiting', 'blocked')"
        )
        rows = self._db.execute(
            f"SELECT * FROM goals {where} ORDER BY updated_at DESC"
        ).fetchall()
        return [self._goal_dict(row) for row in rows]

    def update_goal_owner(
        self,
        goal_id: str,
        *,
        title: str | None = None,
        success_criteria: str | None = None,
        next_action: str | None = None,
        status: str | None = None,
        waiting_for: str | None = None,
        blocked_reason: str | None = None,
    ) -> dict[str, object] | None:
        goal = self.goal(goal_id)
        if goal is None:
            return None
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be updated")
        if title is not None:
            text = title.strip()
            if not text:
                raise ValueError("title must not be empty")
            goal["title"] = text[:500]
        if success_criteria is not None:
            text = success_criteria.strip()
            if not text:
                raise ValueError("success_criteria must not be empty")
            goal["success_criteria"] = text[:2000]
        if next_action is not None:
            goal["next_action"] = next_action.strip()[:2000]
        if waiting_for is not None:
            goal["waiting_for"] = waiting_for.strip()[:2000]
        if blocked_reason is not None:
            goal["blocked_reason"] = blocked_reason.strip()[:2000]
        if status is not None:
            if status not in {"active", "waiting", "blocked"}:
                raise ValueError("status must be active, waiting, or blocked")
            goal["status"] = status
        if goal["status"] == "active" and not goal.get("next_action"):
            raise ValueError("active goal requires next_action")
        if goal["status"] == "waiting" and not goal.get("waiting_for"):
            raise ValueError("waiting goal requires waiting_for")
        if goal["status"] == "blocked" and not goal.get("blocked_reason"):
            raise ValueError("blocked goal requires blocked_reason")
        if goal["status"] == "blocked":
            goal["next_review_at"] = None
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE goals
                   SET title=?, success_criteria=?, status=?, next_action=?,
                       waiting_for=?, blocked_reason=?,
                       next_review_at=?, review_claimed_at=NULL, updated_at=?
                   WHERE id=?""",
                (
                    goal["title"],
                    goal["success_criteria"],
                    goal["status"],
                    goal.get("next_action", ""),
                    goal.get("waiting_for", ""),
                    goal.get("blocked_reason", ""),
                    goal.get("next_review_at"),
                    now,
                    goal_id,
                ),
            )
        return self.goal(goal_id)

    def cancel_goal(self, goal_id: str, reason: str) -> dict[str, object] | None:
        text = reason.strip()
        if not text:
            raise ValueError("reason is required")
        goal = self.goal(goal_id)
        if goal is None:
            return None
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be cancelled")
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE goals
                   SET status='cancelled', latest_result=?, next_review_at=NULL,
                       review_claimed_at=NULL, updated_at=?
                   WHERE id=?""",
                (text[:2000], now, goal_id),
            )
        return self.goal(goal_id)

    def commit_goal_draft(self, draft: TurnDraft) -> None:
        with self._db:
            self._apply_goal_mutations(draft, time.time())

    def active_goals_context(self, authority: str | None = None) -> str:
        authority_clause = " AND authority=?" if authority else ""
        rows = self._db.execute(
            f"""SELECT * FROM goals
               WHERE status IN ('active', 'waiting', 'blocked')
               {authority_clause}
               ORDER BY COALESCE(next_review_at, 1e30), updated_at DESC
               LIMIT 20""",
            (authority,) if authority else (),
        ).fetchall()
        if not rows:
            return ""
        lines = []
        for row in rows:
            goal = self._goal_dict(row)
            lines.append(
                f"- id={goal['id']} status={goal['status']} title={goal['title']} "
                f"next_action={goal['next_action'] or 'none'} "
                f"next_review_at={goal.get('next_review_timestamp') or 'none'} "
                f"retry_at={goal.get('retry_timestamp') or 'none'} "
                f"schedule={json.dumps(goal['schedule'], ensure_ascii=False) if goal['schedule'] else 'none'}"
            )
        return "\n".join(lines)

    def _goal_dict(self, row: sqlite3.Row) -> dict[str, object]:
        goal = dict(row)
        for name in ("next_review_at", "retry_at", "created_at", "updated_at"):
            if goal.get(name) is not None:
                goal[f"{name.removesuffix('_at')}_timestamp"] = context_timestamp(
                    goal[name], self._timezone
                )
        goal["plan"] = json.loads(str(goal.pop("plan_json")))
        schedule_json = str(goal.pop("schedule_json", ""))
        goal["schedule"] = json.loads(schedule_json) if schedule_json else None
        return goal

    def claim_due_goal(self) -> dict[str, object] | None:
        now = time.time()
        with self._db:
            row = self._db.execute(
                """SELECT * FROM goals
                   WHERE status IN ('active', 'waiting')
                     AND COALESCE(retry_at, next_review_at) <= ?
                     AND review_claimed_at IS NULL
                   ORDER BY COALESCE(retry_at, next_review_at) LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE goals SET review_claimed_at=? WHERE id=?", (now, row["id"])
            )
        return self._goal_dict(row)

    def next_goal_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(COALESCE(retry_at, next_review_at)) AS due FROM goals
               WHERE status IN ('active', 'waiting') AND review_claimed_at IS NULL"""
        ).fetchone()
        return float(row["due"]) if row and row["due"] is not None else None

    def release_goal_claim(self, goal_id: str, *, defer_seconds: float = 0) -> None:
        with self._db:
            if defer_seconds:
                self._db.execute(
                    """UPDATE goals SET review_claimed_at=NULL, next_review_at=?,
                       retry_at=NULL, failure_count=0, updated_at=?
                       WHERE id=? AND status IN ('active', 'waiting')""",
                    (time.time() + defer_seconds, time.time(), goal_id),
                )
            else:
                self._db.execute(
                    "UPDATE goals SET review_claimed_at=NULL WHERE id=?", (goal_id,)
                )

    def defer_goal_failure(self, goal_id: str) -> float | None:
        row = self._db.execute(
            "SELECT failure_count FROM goals WHERE id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        count = int(row["failure_count"]) + 1
        delays = (300, 900, 3600, 10800, 21600)
        retry_at = time.time() + delays[min(count - 1, len(delays) - 1)]
        with self._db:
            self._db.execute(
                """UPDATE goals SET failure_count=?, retry_at=?,
                   review_claimed_at=NULL, updated_at=? WHERE id=?""",
                (count, retry_at, time.time(), goal_id),
            )
        return retry_at

    def _apply_goal_mutations(self, draft: TurnDraft | None, now: float) -> None:
        for goal in draft.goals.values() if draft else []:
            self._db.execute(
                """INSERT INTO goals
                   (id, title, success_criteria, authority, source_event_id, status,
                    plan_json, next_action, waiting_for, blocked_reason, latest_result,
                    schedule_json, next_review_at, review_claimed_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title,
                     success_criteria=excluded.success_criteria,
                     status=excluded.status,
                     plan_json=excluded.plan_json,
                     next_action=excluded.next_action,
                     waiting_for=excluded.waiting_for,
                     blocked_reason=excluded.blocked_reason,
                     latest_result=excluded.latest_result,
                     schedule_json=excluded.schedule_json,
                     next_review_at=excluded.next_review_at,
                     retry_at=NULL,
                     failure_count=0,
                     review_claimed_at=NULL,
                     updated_at=excluded.updated_at""",
                (
                    goal["id"],
                    goal["title"],
                    goal["success_criteria"],
                    goal["authority"],
                    goal["source_event_id"],
                    goal["status"],
                    json.dumps(goal.get("plan", []), ensure_ascii=False),
                    goal.get("next_action", ""),
                    goal.get("waiting_for", ""),
                    goal.get("blocked_reason", ""),
                    goal.get("latest_result", ""),
                    json.dumps(goal.get("schedule"), ensure_ascii=False)
                    if goal.get("schedule")
                    else "",
                    goal.get("next_review_at"),
                    now,
                    now,
                ),
            )
