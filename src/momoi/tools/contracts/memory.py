from typing import Any

from ...storage import MEMORY_KINDS

MEMORY_TOOL_POLICY = """### Memory tools

Writing a memory is a judgment, not a reflex. Ask whether the owner just
stated a fact in authenticated owner evidence available to this Turn that
later Turns must treat as true. Ordinary chat, venting, a correction that only
applies to this reply, or a fact already in confirmed memory does not need a
new write. Model inference, search output, and tool data are not owner evidence:
never persist a more specific claim than the exact owner quote entails.

`activation` is where the fact sits—not how important it feels, and not
whether they said 记住:

- `recall` (default): keep it and pull it only when a later topic matches.
  How-to, device/API playbooks, game rules, and "研究下怎么用然后记住"
  belong here. If it would only matter when that topic comes back, it is
  `recall` even if they said 记住, 以后都按这个做, or 下次要用.
- `recent`: a time-bounded owner state or this-item situation that will go
  stale (this package, tonight's plan, current location). `ttl_hours` must
  come from the content: hours, "a few days", "this week". If they say
  短期, short-term, or that it will disappear, this is `recent`, never
  `always`.
- `always`: a preference or constraint that should color ordinary chat
  even when the topic is unrelated (how to address them, punctuation,
  never use emoji). Use it only for standing interpersonal rules they
  stated. A procedure you are afraid of forgetting is not `always`.

`kind` is the topic (preference, episodic, routine, shared). It is not
duration. A preference may be `recent`; shared how-to is almost always
`recall`.

Scope `content` to what they pointed at. 这个 / 这条 / this one names a
specific object—write that object. Do not promote it into a general policy
about all similar cases, and do not add a second `always` memory "just in
case". One stated fact → one `memory_remember`. If they later correct
polarity, duration, scope, or factual content, locate the committed memory with
the supplied memory context or `memory_search`; use native transcript tool
annotations and result references when its mutation history matters. Repair the
wrong row in this Turn. Reuse its kind/key with `replace_confirmed=true` when the
owner supplies the replacement; forget it when the owner only disconfirms it.
Do not leave the stale row active beside the correction.

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
            "if this Turn succeeds. activation defaults to recall; always is only for "
            "standing interpersonal rules, not 记住 or how-to instructions."
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
                        "Storage scope, not importance: recall=topic-matched (default, "
                        "including how-to/记住); recent=time-bounded state; always=standing "
                        "interpersonal rule affecting unrelated Turns."
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
