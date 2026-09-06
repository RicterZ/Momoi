from typing import Any

from ...contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD

AGENDA_TOOL_POLICY = """### Agenda tools

- Every active goal needs a concrete next action and future review time.
- A recurring goal may use an interval or daily `schedule`. Daily times use the
  application's single configured timezone. The runtime computes each next
  review while the Goal remains open.
- `goal_update` keeps a Goal open with its latest state. `goal_finish` closes it as
  successfully completed when its success criteria are satisfied. `goal_cancel`
  closes it without claiming success when it should no longer be pursued.
- During a due Goal review, submit its outcome once using `end_turn.goal`; do not
  separately call goal_update, goal_finish, or goal_cancel.
- Use a Goal for every future action, including a one-time or recurring owner
  notification. Use `next_review_at` once or a recurring `schedule`; describe the
  intended notification in its success criteria and next action. At review time,
  use current context and `send_bubbles`, then call `end_turn` with goal.status
  set to done for a completed one-time Goal or active to keep a recurring one. Never use `sleep` to cross Turns.
- During autonomous Goal review, `send_bubbles` is available only for a useful
  result, a needed decision, or a meaningful failure; otherwise finish the
  autonomous Turn silently. Use separate short `bubbles` when the notification
  has distinct parts; use a single item when it is one thought. Treat each item
  as an owner-visible private-chat bubble governed
  by the shared Style Card and system bubble rules.
"""


def _schedule_schema(description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["interval"]},
                    "every_seconds": {"type": "integer", "minimum": 60},
                },
                "required": ["kind", "every_seconds"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["daily"]},
                    "times": {
                        "type": "array",
                        "description": "Distinct local HH:MM times.",
                        "items": {
                            "type": "string",
                            "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$",
                        },
                        "minItems": 1,
                        "maxItems": 24,
                        "uniqueItems": True,
                    },
                },
                "required": ["kind", "times"],
                "additionalProperties": False,
            },
        ],
    }
    if description:
        schema["description"] = description
    return schema


AGENDA_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "goal_create",
        OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
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
                    "Recurring interval or local daily times; replaces next_review_at."
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
                "latest_result": {
                    "type": "string",
                    "description": (
                        "This execution's checks, actions, sent wording, and angle, "
                        "so the next run can vary it. Exclude owner state supplied "
                        "fresh by memory or conversation."
                    ),
                },
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
            "Close a goal successfully only when all success criteria are achieved."
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
        OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
        "description": (
            "Close a goal without success when abandoned, obsolete, or stopped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"goal_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["goal_id", "reason"],
            "additionalProperties": False,
        },
    },
]


GOAL_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Outcome of the current Goal review; the runtime supplies the Goal ID.",
    "properties": {
        **{
            key: value
            for key, value in AGENDA_TOOL_SPECS[1]["input_schema"]["properties"].items()
            if key not in {"goal_id", "status", "latest_result"}
        },
        "status": {
            "type": "string",
            "enum": ["active", "waiting", "blocked", "done", "cancelled"],
        },
        "result": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "description": "Verified result of this review, or the reason for cancellation.",
        },
    },
    "required": ["status", "result"],
    "additionalProperties": False,
}
