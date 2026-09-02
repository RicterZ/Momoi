import json
import logging
import re
import time

from ..logging_context import log_event, safe_preview
from .turn_workflow import turn_workflow_kind_sql

logger = logging.getLogger(__name__)
RECENT_HEARTBEAT_LIMIT = 6
_HEARTBEAT_RECORD_ACTIVITY = re.compile(r"^Activity: (.*)$", re.MULTILINE)


def _heartbeat_record_activity(content: str) -> str:
    match = _HEARTBEAT_RECORD_ACTIVITY.search(content)
    return match.group(1).strip()[:300] if match else ""


class HeartbeatStateStore:
    def self_state(self) -> dict[str, object]:
        row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("self_state is not initialized")
        return dict(row)

    def self_state_context(self, now: float | None = None) -> str:
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

    def recent_heartbeat_activities(self) -> list[dict[str, str]]:
        workflow = turn_workflow_kind_sql("t")
        rows = self._db.execute(
            f"""SELECT m.content, m.created_at FROM messages AS m
               JOIN turns AS t ON t.id=m.turn_id
               WHERE m.delivery_state='internal'
                 AND {workflow}='heartbeat'
               ORDER BY m.created_at DESC, m.id DESC
               LIMIT ?""",
            (RECENT_HEARTBEAT_LIMIT,),
        ).fetchall()
        items: list[dict[str, str]] = []
        for row in reversed(rows):
            text = _heartbeat_record_activity(str(row["content"] or ""))
            if not text:
                continue
            items.append(
                {
                    "at": self.context_timestamp(row["created_at"]),
                    "text": text,
                }
            )
        return items[-RECENT_HEARTBEAT_LIMIT:]

    def pending_owner_reply(self, now: float | None = None) -> dict[str, object] | None:
        now = time.time() if now is None else now
        row = self._db.execute(
            """SELECT pending_reply_turn_id, pending_reply_expectation,
                      pending_reply_since, pending_reply_last_reason,
                      pending_reply_channel, pending_reply_delay_minutes,
                      pending_reply_next_check_at
               FROM self_state WHERE id=1"""
        ).fetchone()
        if row is None or not str(row["pending_reply_expectation"] or "").strip():
            return None
        since = float(row["pending_reply_since"] or now)
        source_turn = str(row["pending_reply_turn_id"] or "")
        source_messages = [
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
                "delivery_state": str(item["delivery_state"]),
                "timestamp": self.context_timestamp(item["created_at"]),
            }
            for item in self._db.execute(
                """SELECT role, content, created_at, delivery_state FROM messages
                   WHERE turn_id=? AND (role IN ('user', 'event') OR delivery_state IN ('delivered','uncertain'))
                   ORDER BY id""",
                (source_turn,),
            ).fetchall()
        ]
        return {
            "source_turn": source_turn,
            "source_messages": source_messages,
            "expected_information": str(row["pending_reply_expectation"]),
            "reason": str(row["pending_reply_last_reason"] or ""),
            "waiting_since": self.context_timestamp(since),
            "waiting_minutes": max(0, int((now - since) / 60)),
            "delay_minutes": int(row["pending_reply_delay_minutes"] or 0),
            "deadline": self.context_timestamp(row["pending_reply_next_check_at"] or now),
            "channel": str(row["pending_reply_channel"] or ""),
        }

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

