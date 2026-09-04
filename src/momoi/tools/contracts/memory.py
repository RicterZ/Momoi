from typing import Any

from ...storage import MEMORY_KINDS

MEMORY_TOOL_POLICY = """### Memory tools

Writing a memory is a judgment, not a reflex. Ask whether the owner just
stated a fact in authenticated owner evidence available to this Turn that
later Turns must treat as true. Ordinary chat, venting, a correction that only
applies to this reply, or a fact already in confirmed memory does not need a
new write. Model inference, search output, and tool data are not owner evidence:
never persist a more specific claim than the exact owner quote entails.

`activation` controls when the memory enters context. Classify by scope, not
importance or emphasis:

- `recall` (default): retrieve only when the topic matches.
- `recent`: include until an evidence-based `ttl_hours` expires.
- `always`: include every Turn; only for explicit, topic-independent
  interpersonal preferences or constraints.

Topic-dependent knowledge is `recall`; expiring state is `recent`. Do not
promote either to `always`.

`kind` is the topic (preference, episodic, routine, shared). It is not
duration. A preference may be `recent`; shared how-to is almost always
`recall`.

Preserve the owner's exact scope, polarity, duration and conditions. One fact
maps to one memory; never generalize or create defensive duplicates. On a
correction, locate the existing row from supplied context or `memory_search`
and resolve it in this Turn: replace under the same kind/key with
`replace_confirmed=true` when a replacement is supplied; otherwise forget it.
Never leave conflicting rows active. Consult transcript annotations or result
references only when mutation history is needed.

`evidence` is an exact quote. `content` must keep the same polarity and
conditions as that quote (taken vs not taken; only when already picked up).
Canonicalize; do not generalize.
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
                    "description": (
                        "Message id returned with next_content_offset."
                    ),
                },
                "content_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "next_content_offset for the same message_id."
                    ),
                },
            },
            "required": ["episode_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_remember",
        "description": (
            "Stage one memory from an exact authenticated-owner quote. Commits only "
            "if this Turn succeeds. activation defaults to recall; always is limited "
            "to explicit, topic-independent interpersonal rules."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(MEMORY_KINDS),
                    "description": "Topic category, not duration.",
                },
                "key": {
                    "type": "string",
                    "description": "Stable lowercase dotted key; reuse for corrections.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Faithful concise restatement preserving the specific object, "
                        "polarity, and conditions; never broaden 这个/this into a standing rule."
                    ),
                },
                "activation": {
                    "type": "string",
                    "enum": ["recall", "recent", "always"],
                    "description": (
                        "Context scope: recall=topic-matched; recent=until its "
                        "evidence-based TTL expires; always=every Turn for explicit, "
                        "topic-independent interpersonal rules."
                    ),
                },
                "ttl_hours": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 720,
                    "description": (
                        "Required: recent lifetime in hours (1-720, inferred from the "
                        "owner's wording); send 0 for recall/always."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "Exact contiguous quote from one authenticated owner message.",
                },
                "importance": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.5,
                },
                "replace_confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "True only when current owner evidence explicitly replaces the key."
                    ),
                },
            },
            "required": [
                "kind",
                "key",
                "content",
                "evidence",
                "activation",
                "ttl_hours",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_forget",
        "description": (
            "Forget one committed memory only when current authenticated-owner evidence "
            "requests deletion or directly disproves it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                "key": {"type": "string"},
                "evidence": {
                    "type": "string",
                    "description": "Exact contiguous quote from one authenticated owner message.",
                },
            },
            "required": ["kind", "key", "evidence"],
            "additionalProperties": False,
        },
    },
]
