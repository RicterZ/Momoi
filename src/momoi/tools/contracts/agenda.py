from typing import Any

from ...contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD

AGENDA_TOOL_POLICY = """### Agenda tools

Use a persistent Goal as a crontab-like scheduled task for one-time or recurring
work. Record the intended outcome and schedule, then maintain its state as
circumstances change. Work that can finish now needs no Goal.
"""

_REVIEW_TIME_SCHEMA = {
    "type": "string",
    "format": "date-time",
    "pattern": r"T.+(?:Z|[+-]\d{2}:\d{2})$",
}


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
                        "description": "Daily times in the application's configured timezone.",
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
                "title": {"type": "string", "pattern": r"\S"},
                "success_criteria": {"type": "string", "pattern": r"\S"},
                "next_action": {"type": "string", "pattern": r"\S"},
                "next_review_at": {
                    **_REVIEW_TIME_SCHEMA,
                    "description": "Future review time for a one-time Goal.",
                },
                "schedule": _schedule_schema(
                    "Recurrence; the runtime computes the next review."
                ),
            },
            "required": ["title", "success_criteria", "next_action"],
            "oneOf": [
                {"required": ["next_review_at"], "properties": {"schedule": False}},
                {"required": ["schedule"], "properties": {"next_review_at": False}},
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "goal_update",
        "description": (
            "Update an existing open Goal; omitted state fields retain their current values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "waiting", "blocked"]},
                "next_action": {
                    "type": "string",
                    "description": "Next concrete step; active Goals need one.",
                },
                "waiting_for": {
                    "type": "string",
                    "description": "Unmet condition; waiting Goals need one.",
                },
                "blocked_reason": {
                    "type": "string",
                    "description": "Obstacle preventing progress; blocked Goals need one.",
                },
                "latest_result": {
                    "type": "string",
                    "description": (
                        "Concrete checks, actions, and verified outcome. "
                        "Exclude owner state supplied fresh by memory or conversation."
                    ),
                },
                "next_review_at": {
                    **_REVIEW_TIME_SCHEMA,
                    "description": "Future review; required for waiting or non-recurring active. Omit for recurring active.",
                },
                "schedule": _schedule_schema(),
                "clear_schedule": {
                    "type": "boolean",
                    "description": "Remove existing recurrence.",
                },
            },
            "required": ["goal_id", "status"],
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "waiting"}}},
                    "then": {"required": ["next_review_at"]},
                },
                {
                    "if": {
                        "properties": {"clear_schedule": {"const": True}},
                        "required": ["clear_schedule"],
                    },
                    "then": {"properties": {"schedule": False}},
                },
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "goal_finish",
        "description": (
            "Close a Goal successfully when all success criteria are achieved."
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
            "Close a Goal without success when abandoned, obsolete, or stopped."
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
    "description": (
        "Outcome of the current Goal review; the runtime supplies the Goal ID."
    ),
    "properties": {
        **{
            key: value
            for key, value in AGENDA_TOOL_SPECS[1]["input_schema"]["properties"].items()
            if key not in {"goal_id", "status", "latest_result"}
        },
        "next_review_at": {
            **_REVIEW_TIME_SCHEMA,
            "description": (
                "Future review. Active Goals reuse existing recurrence when omitted; "
                "required when no recurrence remains, including after clear_schedule. "
                "Omit for recurring active Goals."
            ),
        },
        "status": {
            "type": "string",
            "enum": ["active", "waiting", "blocked", "done", "cancelled"],
        },
        "result": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "pattern": r"\S",
            "description": (
                "Concrete checks, actions, and verified outcome of this review, or the "
                "reason for cancellation."
            ),
        },
    },
    "required": ["status", "result"],
    "oneOf": [
        {
            "description": "Continue work. Schedule a future review, or reuse the recurring schedule.",
            "properties": {
                "status": {"enum": ["active"]},
                "next_action": {"pattern": r"\S"},
            },
            "required": ["next_action"],
        },
        {
            "description": "Await a condition.",
            "properties": {
                "status": {"enum": ["waiting"]},
                "waiting_for": {"pattern": r"\S"},
                "next_review_at": {"pattern": r"\S"},
            },
            "required": ["waiting_for", "next_review_at"],
        },
        {
            "description": "Cannot proceed.",
            "properties": {
                "status": {"enum": ["blocked"]},
                "blocked_reason": {"pattern": r"\S"},
                "next_review_at": False,
            },
            "required": ["blocked_reason"],
        },
        {
            "description": "done when success criteria are met; cancelled when no longer pursued.",
            "properties": {"status": {"enum": ["done", "cancelled"]}},
            "maxProperties": 2,
        },
    ],
    "allOf": [
        {
            "if": {
                "properties": {"clear_schedule": {"const": True}},
                "required": ["clear_schedule"],
            },
            "then": {"properties": {"schedule": False}},
        },
    ],
    "additionalProperties": False,
}
