from typing import Any

from ..builtin_tools import BUILTIN_TOOL_SPECS
from ..storage import REFLECTION_MEMORY_KINDS

CURL_TOOL_SPEC = next(spec for spec in BUILTIN_TOOL_SPECS if spec["name"] == "curl")

SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Channel-neutral segment type such as text, image, file, video, "
                "audio, reply, link, location, mention, or another type supported "
                "by the active channel."
            ),
        },
        "data": {
            "type": "object",
            "description": (
                "Channel segment data. Text uses text; reply uses id; media uses file "
                "with a local path, HTTP(S) URL, or base64 resource. Other fields "
                "depend on the active channel."
            ),
        },
    },
    "required": ["type", "data"],
    "additionalProperties": False,
}
CHANNEL_MESSAGE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "string",
            "minLength": 1,
            "description": (
                "One complete owner-visible message item. A single line break is "
                "allowed, but blank lines must be separate array items."
            ),
        },
        {
            "type": "object",
            "properties": {
                "segments": {
                    "type": "array",
                    "minItems": 1,
                    "items": SEGMENT_SCHEMA,
                }
            },
            "required": ["segments"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "forward": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": ["string", "integer"]},
                            "nickname": {"type": "string", "minLength": 1},
                            "content": {
                                "oneOf": [
                                    {"type": "string", "minLength": 1},
                                    {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": SEGMENT_SCHEMA,
                                    },
                                ]
                            },
                        },
                        "required": ["nickname", "content"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["forward"],
            "additionalProperties": False,
        },
    ]
}
MOOD_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_-]{0,31}$",
            "description": (
                "Short lowercase mood label. Familiar choices include cheerful, "
                "excited, playful, affectionate, content, proud, hopeful, relieved, "
                "curious, thoughtful, calm, focused, tired, down, frustrated, "
                "worried, anxious, embarrassed, lonely, bored, restless, and angry; "
                "use another concise label when it fits better."
            ),
        },
        "intensity": {"type": "number", "minimum": 0, "maximum": 1},
        "cause": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["state", "intensity", "cause"],
    "additionalProperties": False,
}
MOOD_DECISION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["unchanged"]}
            },
            "required": ["decision"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["updated"]},
                **MOOD_UPDATE_SCHEMA["properties"],
            },
            "required": ["decision", *MOOD_UPDATE_SCHEMA["required"]],
            "additionalProperties": False,
        },
    ]
}
RESPOND_TOOL_SPEC: dict[str, Any] = {
    "name": "respond",
    "description": (
        "Required terminal state update for every conversational Turn, called only "
        "after all tool work and send_message calls are complete and always as the "
        "only tool call in its response. It never sends owner-visible messages."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reply_expectation": {
                "type": "string",
                "maxLength": 300,
                "description": (
                    "What Momoi is genuinely waiting for after the Turn's last visible "
                    "message. Use an empty string when no reply is expected."
                ),
            },
            "mood": MOOD_DECISION_SCHEMA,
        },
        "required": [
            "reply_expectation",
            "mood",
        ],
        "additionalProperties": False,
    },
}

HEARTBEAT_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "activity": {"type": "string", "minLength": 1, "maxLength": 300},
        "result": {"type": "string", "maxLength": 2000},
        "next_check_minutes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1440,
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": [
        "activity",
        "result",
        "next_check_minutes",
        "reason",
    ],
    "additionalProperties": False,
}

REPLY_WAIT_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "continue_waiting": {
            "type": "boolean",
            "description": (
                "Whether Momoi still genuinely wants to wait for the pending reply."
            ),
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["continue_waiting", "reason"],
    "additionalProperties": False,
}


def heartbeat_respond_tool_spec() -> dict[str, Any]:
    schema = RESPOND_TOOL_SPEC["input_schema"]
    return {
        **RESPOND_TOOL_SPEC,
        "description": (
            "Required terminal state update for this autonomous heartbeat Turn, called "
            "only after all tool work and optional send_message calls are complete. "
            "It never sends owner-visible messages. The heartbeat object records "
            "Momoi's activity and schedules her next Turn."
        ),
        "input_schema": {
            **schema,
            "properties": {
                **schema["properties"],
                "heartbeat": HEARTBEAT_STATE_SCHEMA,
            },
            "required": [*schema["required"], "heartbeat"],
        },
    }


def reply_wait_respond_tool_spec() -> dict[str, Any]:
    return {
        "name": "respond",
        "description": (
            "Required terminal state update for a pending-reply wait Turn. It never "
            "sends messages; use send_message first for an optional follow-up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reply_wait": REPLY_WAIT_STATE_SCHEMA,
                "mood": MOOD_DECISION_SCHEMA,
            },
            "required": ["reply_wait", "mood"],
            "additionalProperties": False,
        },
    }


SEND_MESSAGE_TOOL_SPEC: dict[str, Any] = {
    "name": "send_message",
    "description": (
        "Emit non-empty owner-visible beats without ending the Turn. Keep each item "
        "as one complete beat. Text may share a beat with images; file, video, audio, "
        "and record must be their own items, and mixed input is split. After the "
        "result, call respond to close the Turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "minItems": 1,
                "items": CHANNEL_MESSAGE_SCHEMA,
            },
        },
        "required": ["messages"],
        "additionalProperties": False,
    },
}

REPLY_EXPECTATION_CLOSE_SPEC: dict[str, Any] = {
    "name": "reply_expectation_close",
    "description": (
        "Close the cooled owner-reply expectation when the conversation has answered it "
        "or it no longer matters. This only changes private relationship state."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def send_message_tool_spec(
    channel_names: list[str], primary_channel: str
) -> dict[str, Any]:
    return {
        **SEND_MESSAGE_TOOL_SPEC,
        "input_schema": {
            **SEND_MESSAGE_TOOL_SPEC["input_schema"],
            "properties": {
                **SEND_MESSAGE_TOOL_SPEC["input_schema"]["properties"],
                "channel": {
                    "type": "string",
                    "enum": channel_names,
                    "default": primary_channel,
                    "description": (
                        "Delivery channel. Omit it to use the configured primary "
                        f"channel ({primary_channel})."
                    ),
                },
            },
        },
    }


AUTONOMOUS_FINISH_SPEC: dict[str, Any] = {
    "name": "autonomous_finish",
    "description": (
        "Required terminal marker for a Goal review. Call it alone after updating, "
        "finishing, or cancelling the Goal and after any optional owner_notify call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

REFLECTION_FINISH_SPEC: dict[str, Any] = {
    "name": "reflection_finish",
    "description": (
        "Required terminal result for the private daily retrospective. It stores the "
        "reflection record, promotes only durable evidence-backed learning, and "
        "optionally housekeeps the supplied always-on owner-memory inventory and "
        "open conversations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 6000},
            "always_memory_actions": {
                "type": "array",
                "maxItems": 8,
                "description": (
                    "Optional housekeeping of `<always_memory_inventory>`. Empty is valid. "
                    "Use memory_id from that inventory only."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "integer", "minimum": 1},
                        "action": {
                            "type": "string",
                            "enum": [
                                "demote_recent",
                                "demote_recall",
                                "merge",
                                "forget",
                            ],
                        },
                        "merge_into_id": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Required for merge; the surviving always memory.",
                        },
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                            "description": (
                                "Required for merge: one concise restatement of the "
                                "surviving memory. Do not concatenate near-duplicates."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 400,
                        },
                    },
                    "required": ["memory_id", "action", "reason"],
                    "additionalProperties": False,
                },
            },
            "recent_memory_actions": {
                "type": "array",
                "maxItems": 8,
                "description": (
                    "Review active recent memories by memory_id. Use extend when the "
                    "state remains active, promote_recall when it became durable, "
                    "or forget when it is finished or stale."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "integer", "minimum": 1},
                        "action": {
                            "type": "string",
                            "enum": ["extend", "promote_recall", "forget"],
                        },
                        "ttl_hours": {"type": "number", "minimum": 1, "maximum": 168},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                    },
                    "required": ["memory_id", "action", "reason"],
                    "additionalProperties": False,
                },
            },
            "conversation_actions": {
                "type": "array",
                "maxItems": 32,
                "description": (
                    "Optional housekeeping of `<open_conversations>`. Empty is valid. "
                    "Use episode_id from that inventory only. Close a thread only when "
                    "it is finished, expired, or superseded."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "episode_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": "Open or closing conversation episode id.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["close"],
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 400,
                        },
                    },
                    "required": ["episode_id", "action", "reason"],
                    "additionalProperties": False,
                },
            },
            "memories": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(REFLECTION_MEMORY_KINDS),
                        },
                        "key": {
                            "type": "string",
                            "description": "Stable lowercase dot-separated key.",
                        },
                        "content": {"type": "string", "minLength": 1},
                        "evidence": {
                            "type": "string",
                            "description": "Exact contiguous quote from the supplied day record.",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": [
                        "kind",
                        "key",
                        "content",
                        "evidence",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "memories"],
        "additionalProperties": False,
    },
}
