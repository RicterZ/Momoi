import logging
import time
import uuid
from datetime import datetime
from typing import Any

from ..observability.events import log_event
from ..models import ToolCall, TurnDraft
from ..storage import Store
from ..storage.core.scheduling import next_schedule_at, normalize_schedule
from .contracts.agenda import AGENDA_TOOL_SPECS, GOAL_REVIEW_SCHEMA
from .validation import validate_tool_arguments

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
    def has_tool(name: str) -> bool:
        return any(spec["name"] == name for spec in AGENDA_TOOL_SPECS)

    def execute(
        self,
        call: ToolCall,
        draft: TurnDraft,
        *,
        source_event_id: str,
    ) -> dict[str, Any]:
        spec = next((spec for spec in AGENDA_TOOL_SPECS if spec["name"] == call.name), None)
        if spec:
            arguments, error = validate_tool_arguments(call.name, call.arguments, spec["input_schema"])
            if error:
                return error
            call = ToolCall(call.id, call.name, arguments, call.argument_error)
        try:
            if call.name == "goal_create":
                return self._create(call.arguments, draft, source_event_id)
            if call.name == "goal_update":
                return self._update(call.arguments, draft)
            if call.name in {"goal_finish", "goal_cancel"}:
                goal_id = str(call.arguments.get("goal_id") or "")
                if goal_id in draft.goals and self.store.goal(goal_id) is None:
                    del draft.goals[goal_id]
                    return {"ok": True, "state": "withdrawn", "goal_id": goal_id}
                return self._close(call.name, call.arguments, draft)
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
        source_event_id: str,
    ) -> dict[str, Any]:
        title = str(arguments.get("title") or "").strip()
        criteria = str(arguments.get("success_criteria") or "").strip()
        next_action = str(arguments.get("next_action") or "").strip()
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
            "source_event_id": source_event_id,
            "status": "active",
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
        goal = draft.goals.get(goal_id)
        if goal is None:
            goal = self.store.goal(goal_id)
            if goal is None:
                raise ValueError("goal not found")
            draft.goal_bases[goal_id] = goal["updated_at"]
        return dict(goal)

    def rebase(self, draft: TurnDraft) -> None:
        """Drop staged mutations whose goal was written by another Turn meanwhile."""
        for goal_id in list(draft.goals):
            stored = self.store.goal(goal_id)
            current = stored["updated_at"] if stored is not None else None
            if current != draft.goal_bases.get(goal_id):
                del draft.goals[goal_id]
                draft.goal_bases.pop(goal_id, None)

    def _update(self, arguments: dict[str, Any], draft: TurnDraft) -> dict[str, Any]:
        goal_id = str(arguments.get("goal_id") or "")
        goal = self._current(goal_id, draft)
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be updated")
        status = str(arguments.get("status") or "")
        goal["status"] = status
        for field in ("next_action", "waiting_for", "blocked_reason"):
            if field in arguments:
                goal[field] = str(arguments[field] or "")[:2000]
        if "result" in arguments:
            goal["latest_result"] = arguments["result"]
        clear_schedule = arguments.get("clear_schedule", False)
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
        goal.update(
            status="done" if name == "goal_finish" else "cancelled",
            latest_result=value[:2000],
            next_review_at=None,
        )
        draft.goals[goal_id] = goal
        return {"ok": True, "state": "staged", "goal": goal}

    def finish_review(
        self,
        goal_id: str,
        decision: dict[str, Any],
        draft: TurnDraft,
    ) -> dict[str, Any]:
        """Stage the current Goal outcome. The caller ends the Turn only on success."""
        try:
            decision, validation_error = validate_tool_arguments(
                "goal_review", decision, GOAL_REVIEW_SCHEMA
            )
            if validation_error:
                return {**validation_error, "error": "invalid_goal_outcome"}
            assert decision is not None
            status = decision.get("status")
            result = decision.get("result")
            if self._current(goal_id, draft)["status"] in {"done", "cancelled"}:
                raise ValueError("current goal is already closed")
            if status in {"done", "cancelled"}:
                return self._close(
                    "goal_finish" if status == "done" else "goal_cancel",
                    {
                        "goal_id": goal_id,
                        "result" if status == "done" else "reason": result,
                    },
                    draft,
                )
            arguments = {
                key: value for key, value in decision.items() if key != "result"
            }
            arguments.update(goal_id=goal_id, result=result)
            return self._update(arguments, draft)
        except (TypeError, ValueError) as error:
            return {"ok": False, "error": "invalid_goal_outcome", "message": str(error)}
