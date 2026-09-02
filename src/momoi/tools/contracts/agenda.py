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
- When reviewing a due goal, update, finish, or cancel it before the Turn ends.
- Use a Goal for every future action, including a one-time or recurring owner
  notification. Use `next_review_at` once or a recurring `schedule`; describe the
  intended notification in its success criteria and next action. At review time,
  use current context and `send_bubbles`, then finish a one-time Goal or update a
  recurring one. Never use `sleep` to cross Turns.
- During autonomous Goal review, `send_bubbles` is available only for a useful
  result, a needed decision, or a meaningful failure; otherwise finish the
  autonomous Turn silently. Use separate short `bubbles` when the notification
  has distinct parts; use a single item when it is one thought. Treat each item
  as an owner-visible private-chat bubble governed
  by the shared Style Card and system bubble rules. Give it a stable category
  `key`; use urgent priority only for a decision or failure that should bypass
  normal quiet rules.
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
                        "description": "One or more distinct local times in HH:MM format.",
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
                    "Recurring interval or daily schedule in the configured app "
                    "timezone. Use instead of next_review_at."
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
                        "What this execution did: what you checked, what you "
                        "sent, and which angle you used so the next run can "
                        "vary it. Keep it to your own actions and wording. The "
                        "owner's situation reaches every review fresh through "
                        "memory and conversation, so leave it out here; a copy "
                        "stored on the Goal keeps its own age and outlives the "
                        "situation it described."
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
        OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
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
]

AUTONOMOUS_SEND_BUBBLES_SPEC: dict[str, Any] = {
    "name": "send_bubbles",
    "description": (
        "Send one useful notification to the owner from an autonomous Turn. "
        "Check the supplied current conversation first; do not notify when the "
        "result is already covered or stale. "
        "Use bubbles as separate short conversational beats; do not "
        "pack independent sentences into one item."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bubbles": {
                "type": "array",
                "minItems": 1,
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
        "required": ["bubbles", "reason", "key"],
        "additionalProperties": False,
    },
}
