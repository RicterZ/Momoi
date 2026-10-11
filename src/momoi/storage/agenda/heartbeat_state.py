import json
import logging
import time

from ...observability.events import log_event
from ...observability.values import safe_preview

logger = logging.getLogger(__name__)


class HeartbeatStateStore:
    def self_state(self) -> dict[str, object]:
        row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("self_state is not initialized")
        return dict(row)

    def affinity_reviews(self):
        rows = self._db.execute("SELECT payload_json FROM turn_journal WHERE item_type='affinity_review' ORDER BY created_at DESC LIMIT 40").fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def affinity_daily_increases(self):
        today = self.context_timestamp(time.time())[:10]
        rows = self._db.execute("SELECT created_at,payload_json FROM turn_journal WHERE item_type='affinity_review'").fetchall()
        return sum(1 for row in rows if self.context_timestamp(row[0])[:10] == today
                   and (review := json.loads(row[1])).get("allowed")
                   and review.get("delta", 0) > 0 and not review.get("settlement", False))

    def apply_affinity_review(self, turn_id, current, delta, arguments, decision):
        with self._db:
            if self.self_state()["affinity"] != current:
                return {"ok": False, "allowed": False, "reason": "审批期间好感度已变化，请重新申请。"}
            if decision["allowed"] and delta > 0 and not arguments.get("settlement") and self.affinity_daily_increases() >= 3:
                return {"ok": False, "allowed": False, "reason": "今日已成功增加好感度 3 次，普通阶段不能再增加；每日复盘结算不占额度。"}
            if decision["allowed"]:
                changed = self._db.execute("UPDATE self_state SET affinity=? WHERE id=1 AND affinity=?", (current + delta, current))
                if changed.rowcount != 1:
                    return {"ok": False, "allowed": False, "reason": "审批期间好感度已变化，请重新申请。"}
            result = {"ok": True, **decision, "value": current + delta if decision["allowed"] else current}
            self._append_turn_journal(turn_id, "affinity_review", {"before": current, "delta": delta, "settlement": bool(arguments.get("settlement")), "settlement_date": arguments.get("settlement_date"), "amount": arguments["amount"], "planner_reason": arguments["reason"], "evidence": arguments["evidence"], "dialogue": [{"role": row["role"], "content": row["content"]} for row in arguments.get("dialogue", [])], **result},
                                      visibility="internal", trust="runtime", created_at=time.time())
        return result

    def self_state_context(self, now: float | None = None, *, affinity_enabled: bool = False) -> str:
        now = time.time() if now is None else now
        state = self.self_state()

        def timestamp(value: object) -> str | None:
            return self.context_timestamp(value) if value is not None else None

        return json.dumps(
            {
                "mood": {
                    "state": state["mood_state"],
                    "intensity": state["mood_intensity"],
                    "cause": state["mood_cause"],
                    "updated_at": timestamp(state["mood_updated_at"]),
                    "age_minutes": max(
                        0, int((now - float(state["mood_updated_at"])) / 60)
                    ),
                },
                **({"affinity": state["affinity"]} if affinity_enabled else {}),
                "activity": {
                    "text": state["activity"],
                    "result": state["activity_result"],
                    "since": timestamp(state["activity_since"]),
                },
                "last_heartbeat_at": timestamp(state["last_heartbeat_at"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _apply_mood_update(
        self, update: dict[str, object] | None, now: float
    ) -> None:
        if update is None:
            return
        previous = self._db.execute(
            "SELECT mood_state, mood_intensity FROM self_state WHERE id=1"
        ).fetchone()
        self._db.execute(
            """UPDATE self_state
               SET mood_state=?, mood_intensity=?, mood_cause=?,
                   mood_updated_at=?, updated_at=? WHERE id=1""",
            (
                update["state"],
                update["intensity"],
                str(update["cause"])[:300],
                now,
                now,
            ),
        )
        log_event(
            logger,
            logging.DEBUG,
            "mood_changed",
            previous_state=previous["mood_state"] if previous else "unknown",
            state=update["state"],
            intensity=round(float(update["intensity"]), 2),
            cause=safe_preview(update["cause"], 300),
        )
