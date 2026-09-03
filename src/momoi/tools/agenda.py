import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

from ..emotions import EMOTION_PREFIX, emotion_slug
from ..observability.events import log_event
from ..models import ToolCall, TurnDraft
from ..storage import Store
from ..storage.scheduling import next_schedule_at, normalize_schedule
from .contracts.agenda import AGENDA_TOOL_SPECS

logger = logging.getLogger(__name__)


def _future_timestamp(value: Any, field: str) -> float:
    text = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    timestamp = parsed.timestamp()
    if timestamp <= time.time():
        raise ValueError(f"{field} must be in the future")
    return timestamp


class AgendaTools:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def has_tool(name: str, *, allow_notify: bool) -> bool:
        return any(spec["name"] == name for spec in AGENDA_TOOL_SPECS) or (
            allow_notify and name == "send_bubbles"
        )

    def execute(
        self,
        call: ToolCall,
        draft: TurnDraft,
        *,
        authority: str,
        source_event_id: str,
        allow_notify: bool,
    ) -> dict[str, Any]:
        try:
            if call.name == "goal_create":
                return self._create(call.arguments, draft, authority, source_event_id)
            if call.name == "goal_update":
                return self._update(call.arguments, draft)
            if call.name in {"goal_finish", "goal_cancel"}:
                return self._close(call.name, call.arguments, draft)
            if call.name == "send_bubbles" and allow_notify:
                return self._notify(call.arguments, draft)
            return {"ok": False, "error": "tool_not_allowed"}
        except (TypeError, ValueError) as error:
            return {
                "ok": False,
                "error": "invalid_arguments",
                "message": str(error)[:500],
            }
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "agenda_tool_failure",
                tool_name=call.name,
                error_type=type(error).__name__,
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "agenda_operation_failed",
                "message": f"Agenda operation failed: {type(error).__name__}.",
                "upstream_error_type": type(error).__name__,
            }

    def _create(
        self,
        arguments: dict[str, Any],
        draft: TurnDraft,
        authority: str,
        source_event_id: str,
    ) -> dict[str, Any]:
        title = str(arguments.get("title") or "").strip()
        criteria = str(arguments.get("success_criteria") or "").strip()
        next_action = str(arguments.get("next_action") or "").strip()
        if not all((title, criteria, next_action)):
            raise ValueError("title, success_criteria, and next_action are required")
        goal_id = uuid.uuid4().hex
        schedule_value = arguments.get("schedule")
        review_value = str(arguments.get("next_review_at") or "").strip()
        schedule = (
            normalize_schedule(schedule_value) if schedule_value is not None else None
        )
        if schedule is not None and review_value:
            raise ValueError("use schedule or next_review_at, not both")
        next_review_at = (
            next_schedule_at(schedule, self.store.timezone)
            if schedule is not None
            else _future_timestamp(review_value, "next_review_at")
        )
        goal = {
            "id": goal_id,
            "title": title[:500],
            "success_criteria": criteria[:2000],
            "authority": authority,
            "source_event_id": source_event_id,
            "status": "active",
            "plan": [str(item)[:1000] for item in arguments.get("plan", [])][:50],
            "next_action": next_action[:2000],
            "waiting_for": "",
            "blocked_reason": "",
            "latest_result": "",
            "schedule": schedule,
            "next_review_at": next_review_at,
        }
        draft.goals[goal_id] = goal
        return {"ok": True, "state": "staged", "goal": goal}

    def _current(self, goal_id: str, draft: TurnDraft) -> dict[str, Any]:
        goal = draft.goals.get(goal_id) or self.store.goal(goal_id)
        if goal is None:
            raise ValueError("goal not found")
        return dict(goal)

    def _update(self, arguments: dict[str, Any], draft: TurnDraft) -> dict[str, Any]:
        goal_id = str(arguments.get("goal_id") or "")
        goal = self._current(goal_id, draft)
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be updated")
        status = str(arguments.get("status") or "")
        if status not in {"active", "waiting", "blocked"}:
            raise ValueError("status must be active, waiting, or blocked")
        goal["status"] = status
        for field in ("next_action", "waiting_for", "blocked_reason", "latest_result"):
            if field in arguments:
                goal[field] = str(arguments[field] or "")[:2000]
        if "plan" in arguments:
            goal["plan"] = [str(item)[:1000] for item in arguments["plan"]][:50]
        clear_schedule = arguments.get("clear_schedule", False)
        if not isinstance(clear_schedule, bool):
            raise ValueError("clear_schedule must be boolean")
        if clear_schedule:
            goal["schedule"] = None
        if "schedule" in arguments:
            if clear_schedule:
                raise ValueError("schedule and clear_schedule cannot be combined")
            goal["schedule"] = normalize_schedule(arguments["schedule"])
        if status in {"active", "waiting"}:
            if status == "active" and goal.get("schedule"):
                if str(arguments.get("next_review_at") or "").strip():
                    raise ValueError(
                        "recurring active goal does not accept next_review_at"
                    )
                goal["next_review_at"] = next_schedule_at(
                    goal["schedule"], self.store.timezone
                )
            else:
                goal["next_review_at"] = _future_timestamp(
                    arguments.get("next_review_at"), "next_review_at"
                )
        else:
            goal["next_review_at"] = None
        if status == "active" and not goal.get("next_action"):
            raise ValueError("active goal requires next_action")
        if status == "waiting" and not goal.get("waiting_for"):
            raise ValueError("waiting goal requires waiting_for")
        if status == "blocked" and not goal.get("blocked_reason"):
            raise ValueError("blocked goal requires blocked_reason")
        draft.goals[goal_id] = goal
        return {"ok": True, "state": "staged", "goal": goal}

    def _close(
        self, name: str, arguments: dict[str, Any], draft: TurnDraft
    ) -> dict[str, Any]:
        goal_id = str(arguments.get("goal_id") or "")
        goal = self._current(goal_id, draft)
        field = "result" if name == "goal_finish" else "reason"
        value = str(arguments.get(field) or "").strip()
        if not value:
            raise ValueError(f"{field} is required")
        goal.update(
            status="done" if name == "goal_finish" else "cancelled",
            latest_result=value[:2000],
            next_review_at=None,
        )
        draft.goals[goal_id] = goal
        return {"ok": True, "state": "staged", "goal": goal}

    def _notify(self, arguments: dict[str, Any], draft: TurnDraft) -> dict[str, Any]:
        raw_bubbles = arguments.get("bubbles")
        reason = str(arguments.get("reason") or "").strip()
        key = str(arguments.get("key") or "").strip()
        priority = str(arguments.get("priority", "normal"))
        if (
            not isinstance(raw_bubbles, list)
            or not raw_bubbles
            or any(
                not isinstance(item, str) or not item.strip() or len(item.strip()) > 500
                for item in raw_bubbles
            )
            or not reason
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", key)
        ):
            raise ValueError("bubbles, reason, and a stable lowercase key are required")
        if priority not in {"normal", "urgent"}:
            raise ValueError("priority must be normal or urgent")
        bubbles = [item.strip() for item in raw_bubbles]
        for bubble in bubbles:
            if EMOTION_PREFIX not in bubble:
                continue
            slug = emotion_slug(bubble)
            if slug is None:
                raise ValueError("emotion directive must be a standalone bubble")
            if self.store.emotion(slug) is None:
                raise ValueError("notification contains an unknown emotion slug")
        draft.notification_messages = bubbles
        draft.notification_key = key
        draft.notification_priority = priority
        draft.notification_reason = reason[:500]
        return {
            "ok": True,
            "state": "staged",
            "bubbles": len(draft.notification_messages),
        }
