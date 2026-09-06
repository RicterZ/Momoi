from typing import Any


MEMORY_TOOL_POLICY = """### Memory tools

Use memory_operation only when authenticated owner evidence warrants adding,
correcting, or forgetting memory. Do not record every message. Submit the exact
owner quote and a faithful, scoped fact or description of what to forget.
Use type=add for a new fact, replace for a correction, forget for deletion or
explicit disproof. When a target memory_id is already shown, supply target_id;
otherwise describe the subject. Do not invent IDs, keys, TTLs, or fetch old
memories just to repeat them as arguments: the runtime attaches the memories
already supplied through context, recall, and memory_search.

Accepted operations are saved with this Turn and reviewed privately afterward.
They are not yet effective memory changes. Do not resubmit an accepted operation,
claim that deletion is complete, or treat a pending candidate as confirmed memory.
The background review handles classification, activation, expiry, and duplicates.
Preserve the owner's polarity, object, conditions, and duration in content.
Search memory only for an unresolved conversational need; use Episode originals
when summaries cannot resolve evidence or wording.
"""


MEMORY_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            "Search committed memory for earlier facts, people, preferences, events, "
            "or vague references not already resolved by supplied context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Concise subject; use `|` for alternative names of the same subject."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 6,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "episode_search",
        "description": (
            "Search archived Episodes by keyword or time; empty query browses by time. "
            "Returns paginated summaries and evidence locations, not raw messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Concise subject, optionally with `|` aliases; empty browses "
                        "time_range chronologically."
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        "Window; defaults to 30 days. Use all only when older history matters."
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
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "episode_read",
        "description": (
            "Read paginated raw messages for an Episode id from recall or episode_search. "
            "Returns turn_id, Episode ordinal, role, time, delivery state, and content. Use the "
            "smallest time range; expand only when its summary cannot settle exact "
            "wording, chronology, or evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Episode id from recall or episode_search.",
                },
                "before_ordinal": {
                    "type": "integer",
                    "minimum": 2,
                    "description": (
                        "next_before_ordinal for an older page; omit for newest."
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        "Exact message-time window; prefer a narrow range because raw "
                        "messages are verbose."
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
                "message_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": ("Message id returned with next_content_offset."),
                },
                "content_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": ("next_content_offset for the same message_id."),
                },
            },
            "required": ["episode_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_operation",
        "description": (
            "Submit an add, replace, or forget request supported by a current owner quote. "
            "The runtime attaches recalled memories and conversation; private review runs "
            "after this Turn commits. Acceptance does not mean the change is effective. "
            "Do not repeat an accepted request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["add", "replace", "forget"]},
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "New scoped fact for add/replace; subject to forget for forget. Preserve temporal conditions.",
                },
                "evidence": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "Exact contiguous quote from a current authenticated owner message.",
                },
                "target_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional memory_id already displayed in this Turn. Omit when unknown.",
                },
            },
            "required": ["type", "content", "evidence"],
            "additionalProperties": False,
        },
    },
]
