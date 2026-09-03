from typing import Any

THINKING_TOOL_POLICY = """### Thinking tools

Use `thinking_search` and `thinking_read` when the owner asks why Momoi did
or did not do something, or how a recent Turn decided. These records are
fallible traces of past model calls, not current policy or owner-visible
delivery. Outbox and conversation facts take precedence. Do not dump raw
thinking to the owner; give conclusions and necessary evidence.
"""

THINKING_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "thinking_search",
        "description": (
            "Search recorded model thinking by Turn, keyword, or time. Returns "
            "compact excerpts, not full reasoning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "turn_id": {
                    "type": "string",
                    "description": "Exact Turn id.",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Exact keyword or `|`-separated alternatives."
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        "Window; defaults to 30 days without turn_id."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["recent", "range", "all"],
                        },
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3650,
                        },
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                "stage": {
                    "type": "string",
                    "description": (
                        "Call stage, e.g. owner, webhook, heartbeat, goal, reflection."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "cursor": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Offset returned as next_cursor.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "thinking_read",
        "description": (
            "Read thinking for a Turn from thinking_search; pass call_id for one call, "
            "or omit it for all calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "turn_id": {
                    "type": "string",
                    "minLength": 1,
                },
                "call_id": {
                    "type": "string",
                    "description": "Call id from thinking_search.",
                },
            },
            "required": ["turn_id"],
            "additionalProperties": False,
        },
    },
]
