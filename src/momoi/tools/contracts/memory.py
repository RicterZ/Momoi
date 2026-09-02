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
            "Search Momoi's committed long-term memory. Use when the user refers "
            "to prior facts, people, preferences, events, or vague earlier context "
            "that is not already present in the supplied context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Concise subject or phrase to retrieve. Use `|`-separated "
                        "parallel aliases when the same subject may be worded differently."
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
            "Search archived conversation Episodes. Supports keyword search, "
            "time-range browsing with an empty query, and paginated results. "
            "Returns compact summaries and evidence locations, not raw messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Optional concise subject or phrase, with `|`-separated "
                        "parallel aliases when useful. "
                        "Use an empty string to browse Episodes chronologically "
                        "within time_range."
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        "Optional search window. Default is the last 30 days. "
                        "Use kind=all only when older history is necessary."
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
            "Given an Episode id, return a paginated raw message list for its linked "
            "Turns. Each message includes turn_id, Episode ordinal, role, timestamp, "
            "delivery state, and content. Use an id from automatic Episode recall or "
            "episode_search. Narrow time_range to the smallest useful window: "
            "raw messages are verbose. Read broader or older pages only when the "
            "Episode summary is insufficient or exact wording is needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "Episode id returned by automatic recall or episode_search."
                    ),
                },
                "before_ordinal": {
                    "type": "integer",
                    "minimum": 2,
                    "description": (
                        "For an older page, pass next_before_ordinal from the "
                        "previous result. Omit it for the newest page."
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        "Optional exact message-time window. Prefer kind=range with "
                        "a narrow from/to interval. recent/all or a wide range may "
                        "return many raw messages and must be used cautiously."
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
                        "Read another chunk of one oversized archived message. Use "
                        "the id returned with next_content_offset."
                    ),
                },
                "content_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Character offset returned as next_content_offset for the "
                        "same message_id."
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
            "Stage one memory supported by an exact quote from authenticated owner "
            "evidence available to this Turn. Default activation is recall. Use "
            "always only for a standing "
            "rule that should color ordinary chat even off-topic; 记住 and how-to "
            "playbooks are recall. The write commits only when this turn finishes "
            "successfully."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(MEMORY_KINDS),
                    "description": (
                        "Topic category such as episodic, preference, or routine. "
                        "This is not duration. Use activation for recall, recent, or always."
                    ),
                },
                "key": {
                    "type": "string",
                    "description": "Stable lowercase dot-separated key; reuse it for corrections.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Faithful concise restatement of what they pointed at. "
                        "Keep the specific object, polarity, and conditions. "
                        "Do not turn 这个/this into a standing rule about all similar cases."
                    ),
                },
                "activation": {
                    "type": "string",
                    "enum": ["recall", "recent", "always"],
                    "description": (
                        "Where this sits, not how important it feels. "
                        "recall (default): keep for later search; how-to, device/API, "
                        "and 记住怎么用. recent: time-bounded state or this-item "
                        "situation; required when they say short-term or it will expire. "
                        "always: standing interpersonal rule that should color every "
                        "Turn even off-topic. 记住 / 以后要用 is not always."
                    ),
                },
                "ttl_hours": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 720,
                    "description": (
                        "Required. For recent, hours this state should stay active "
                        "(1 to 720), read from the content: a few days is about 72-96. "
                        "For always or recall, send 0; the value is ignored."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Exact contiguous quote from one authenticated owner message "
                        "available to this Turn."
                    ),
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
                        "True only when the current user message explicitly confirms "
                        "this value replaces any existing value for the same key."
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
            "Forget one committed long-term memory when the authenticated user "
            "explicitly requested it or directly disconfirmed that stored fact in "
            "owner evidence available to this Turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                "key": {"type": "string"},
                "evidence": {
                    "type": "string",
                    "description": (
                        "Exact contiguous quote from one authenticated owner message "
                        "available to this Turn."
                    ),
                },
            },
            "required": ["kind", "key", "evidence"],
            "additionalProperties": False,
        },
    },
]
