import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

from .emotions import EMOTION_PREFIX, emotion_slug
from .logging_context import log_event
from .models import ToolCall, TurnDraft
from .storage import Store
from .storage.scheduling import next_schedule_at, normalize_schedule

logger = logging.getLogger(__name__)


AGENDA_TOOL_POLICY = """### Agenda tools

- Every active goal needs a concrete next action and future review time.
- A recurring goal may use an interval or daily `schedule`. The runtime computes
  each next review while the Goal remains open.
- `goal_update` keeps a Goal open with its latest state. `goal_finish` closes it as
  successfully completed when its success criteria are satisfied. `goal_cancel`
  closes it without claiming success when it should no longer be pursued.
- When reviewing a due goal, update, finish, or cancel it before the Turn ends.
- Use `reminder_create` for a one-time reminder. Its `text` is the exact message
  delivered at `fire_at`, without another LLM call. A fixed recurring reminder
  may use an interval or daily `schedule` instead. Use `reminder_cancel` to cancel
  it. Do not use `sleep` or a Goal for a reminder whose text needs no new research.
- Submit independent reminder calls together in one response.
- `owner_notify` is available only during autonomous work. Use it only for a
  useful result, a needed decision, or a meaningful failure; otherwise finish
  the autonomous Turn silently. Give it one to three separate short `messages`
  beats when the notification has distinct parts; use a single item when it is
  one thought. Treat each item as an owner-visible private-chat bubble governed
  by the shared Style Card and system bubble rules. Give it a stable category
  `key`; use urgent priority only for a decision or failure that should bypass
  normal quiet rules.
"""


def _schedule_schema(description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["interval", "daily"]},
            "timezone": {"type": "string"},
            "every_seconds": {"type": "integer", "minimum": 60},
            "at": {"type": "string"},
        },
        "required": ["kind", "timezone"],
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


AGENDA_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "goal_create",
        "description": "Persist work that must continue in a future Turn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "success_criteria": {"type": "string"},
                "plan": {"type": "array", "items": {"type": "string"}},
                "next_action": {"type": "string"},
                "next_review_at": {
                    "type": "string",
                    "description": "ISO 8601 timestamp with timezone.",
                },
                "schedule": _schedule_schema(
                    "Recurring interval or daily local-time schedule. Use instead "
                    "of next_review_at."
                ),
            },
            "required": ["title", "success_criteria", "next_action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "goal_update",
        "description": "Update an existing active, waiting, or blocked goal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "waiting", "blocked"]},
                "plan": {"type": "array", "items": {"type": "string"}},
                "next_action": {"type": "string"},
                "waiting_for": {"type": "string"},
                "blocked_reason": {"type": "string"},
                "latest_result": {"type": "string"},
                "next_review_at": {"type": "string"},
                "schedule": _schedule_schema(),
                "clear_schedule": {"type": "boolean"},
            },
            "required": ["goal_id", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "goal_finish",
        "description": (
            "Permanently close a goal as successfully completed because its overall "
            "success criteria are fully achieved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"goal_id": {"type": "string"}, "result": {"type": "string"}},
            "required": ["goal_id", "result"],
            "additionalProperties": False,
        },
    },
    {
        "name": "goal_cancel",
        "description": (
            "Permanently close a goal without success because it is abandoned, "
            "obsolete, or explicitly stopped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"goal_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["goal_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reminder_create",
        "description": "Schedule one exact message to be delivered once in the future.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "fire_at": {
                    "type": "string",
                    "description": "Future ISO 8601 timestamp with timezone.",
                },
                "schedule": _schedule_schema(
                    "Recurring interval or daily local-time schedule."
                ),
            },
            "required": ["text"],
            "oneOf": [
                {"required": ["fire_at"]},
                {"required": ["schedule"]},
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "reminder_cancel",
        "description": "Cancel a pending one-time reminder.",
        "input_schema": {
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
            "additionalProperties": False,
        },
    },
]

OWNER_NOTIFY_SPEC: dict[str, Any] = {
    "name": "owner_notify",
    "description": (
        "Send one useful notification to the owner from an autonomous Turn. "
        "Check the supplied current conversation first; do not notify when the "
        "result is already covered or stale. "
        "Use messages as one to three separate short conversational beats; do not "
        "pack independent sentences into one item."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "reason": {"type": "string"},
            "key": {
                "type": "string",
                "description": "Stable lowercase category used for notification cooldown.",
            },
            "priority": {
                "type": "string",
                "enum": ["normal", "urgent"],
                "default": "normal",
            },
        },
        "required": ["messages", "reason", "key"],
        "additionalProperties": False,
    },
}


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
            allow_notify and name == "owner_notify"
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
            if call.name == "reminder_create":
                return self._create_reminder(
                    call.arguments, draft, source_event_id
                )
            if call.name == "reminder_cancel":
                return self._cancel_reminder(call.arguments, draft)
            if call.name == "owner_notify" and allow_notify:
                return self._notify(call.arguments, draft)
            return {"ok": False, "error": "tool_not_allowed"}
        except (TypeError, ValueError) as error:
            return {"ok": False, "error": "invalid_arguments", "message": str(error)[:500]}
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
            normalize_schedule(schedule_value)
            if schedule_value is not None
            else None
        )
        if schedule is not None and review_value:
            raise ValueError("use schedule or next_review_at, not both")
        next_review_at = (
            next_schedule_at(schedule)
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
                    raise ValueError("recurring active goal does not accept next_review_at")
                goal["next_review_at"] = next_schedule_at(goal["schedule"])
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

    def _create_reminder(
        self,
        arguments: dict[str, Any],
        draft: TurnDraft,
        source_event_id: str,
    ) -> dict[str, Any]:
        text = str(arguments.get("text") or "").strip()
        if not text or len(text) > 2000:
            raise ValueError("text must contain 1 to 2000 characters")
        reminder_id = uuid.uuid4().hex
        has_fire_at = bool(str(arguments.get("fire_at") or "").strip())
        has_schedule = arguments.get("schedule") is not None
        if has_fire_at == has_schedule:
            raise ValueError("use exactly one of fire_at or schedule")
        schedule = (
            normalize_schedule(arguments["schedule"])
            if has_schedule
            else None
        )
        reminder = {
            "id": reminder_id,
            "text": text,
            "source_event_id": source_event_id,
            "status": "pending",
            "fire_at": (
                next_schedule_at(schedule)
                if schedule is not None
                else _future_timestamp(arguments.get("fire_at"), "fire_at")
            ),
            "schedule": schedule,
        }
        draft.reminders[reminder_id] = reminder
        return {"ok": True, "state": "staged", "reminder": reminder}

    def _cancel_reminder(
        self, arguments: dict[str, Any], draft: TurnDraft
    ) -> dict[str, Any]:
        reminder_id = str(arguments.get("reminder_id") or "").strip()
        reminder = draft.reminders.get(reminder_id) or self.store.reminder(reminder_id)
        if reminder is None:
            raise ValueError("reminder not found")
        if reminder["status"] != "pending":
            raise ValueError("only a pending reminder can be cancelled")
        cancelled = dict(reminder)
        cancelled["status"] = "cancelled"
        draft.reminders[reminder_id] = cancelled
        return {"ok": True, "state": "staged", "reminder": cancelled}

    def _close(self, name: str, arguments: dict[str, Any], draft: TurnDraft) -> dict[str, Any]:
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
        raw_messages = arguments.get("messages")
        reason = str(arguments.get("reason") or "").strip()
        key = str(arguments.get("key") or "").strip()
        priority = str(arguments.get("priority", "normal"))
        if (
            not isinstance(raw_messages, list)
            or not 1 <= len(raw_messages) <= 3
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > 500
                for item in raw_messages
            )
            or not reason
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9_.-]{0,99}", key
            )
        ):
            raise ValueError("messages, reason, and a stable lowercase key are required")
        if priority not in {"normal", "urgent"}:
            raise ValueError("priority must be normal or urgent")
        messages = [item.strip() for item in raw_messages]
        for message in messages:
            if message.startswith(EMOTION_PREFIX):
                slug = emotion_slug(message)
                if slug is None or self.store.emotion(slug) is None:
                    raise ValueError("notification contains an unknown emotion slug")
        draft.notification_messages = messages
        draft.notification_key = key
        draft.notification_priority = priority
        draft.notification_reason = reason[:500]
        return {"ok": True, "state": "staged", "messages": len(draft.notification_messages)}
