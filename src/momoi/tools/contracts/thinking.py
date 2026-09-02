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
            "Search Momoi's recorded model-call thinking. Supports turn_id, "
            "keyword, and time_range. Monthly storage is resolved automatically. "
            "Returns compact excerpts, not full reasoning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "turn_id": {
                    "type": "string",
                    "description": "Exact Turn id when the owner refers to one Turn.",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional exact keyword or `|`-separated OR alternatives "
                        "likely to occur in recorded thinking."
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        "Optional search window. Default is the last 30 days "
                        "when turn_id is omitted."
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
                        "Optional call stage such as owner, webhook, "
                        "heartbeat, goal, or reflection."
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
            "Read recorded thinking for one Turn returned by thinking_search. "
            "Pass call_id to read one call; omit it to read every call in the Turn."
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
                    "description": "Optional call id from thinking_search.",
                },
            },
            "required": ["turn_id"],
            "additionalProperties": False,
        },
    },
]
