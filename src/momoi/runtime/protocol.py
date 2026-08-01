from typing import Any

from ..builtin_tools import BUILTIN_TOOL_SPECS
from ..storage import MOOD_STATES, REFLECTION_MEMORY_KINDS

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
        {"type": "string", "minLength": 1},
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
MOOD_TRANSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": sorted(MOOD_STATES)},
        "intensity": {"type": "number", "minimum": 0, "maximum": 1},
        "cause": {"type": "string", "minLength": 1, "maxLength": 300},
        "duration_minutes": {
            "type": "integer",
            "minimum": 5,
            "maximum": 1440,
        },
    },
    "required": ["state", "intensity", "cause", "duration_minutes"],
    "additionalProperties": False,
}
MOOD_DECISION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["keep"]}},
            "required": ["action"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["transition"]},
                **MOOD_TRANSITION_SCHEMA["properties"],
            },
            "required": ["action", *MOOD_TRANSITION_SCHEMA["required"]],
            "additionalProperties": False,
        },
    ]
}
DELIVERY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "description": (
        "Brief private expression plan made before messages: decide whether a visible "
        "reply is natural; if speaking, choose the voice, scale, message rhythm, and "
        "any emotion reaction and its position. It is not shown to the owner."
    ),
}

RESPOND_TOOL_SPEC: dict[str, Any] = {
    "name": "respond",
    "description": (
        "Required terminal decision for every conversational Turn, called only after "
        "all tool work is complete. It may carry no message when silence is natural, "
        "and does not replace useful live check-ins through send_message during "
        "substantial work."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "delivery": DELIVERY_SCHEMA,
            "messages": {
                "type": "array",
                "items": CHANNEL_MESSAGE_SCHEMA,
            },
            "expects_reply": {
                "type": "boolean",
                "description": (
                    "Whether Momoi, guided by her Soul, relationship with the owner, "
                    "and current context, will genuinely wait for, look forward to, or "
                    "keep attention on the owner's reply to these final messages."
                ),
            },
            "reply_expectation": {
                "type": "string",
                "maxLength": 300,
                "description": (
                    "Briefly state what Momoi is waiting for when expects_reply is true; "
                    "otherwise use an empty string."
                ),
            },
            "continuity": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "open_loops": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string"},
                    },
                    "pending_commitments": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string"},
                    },
                    "short_term_facts": {
                        "type": "array",
                        "maxItems": 12,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "expires_at": {
                                    "type": "string",
                                    "description": "ISO 8601 timestamp with timezone.",
                                },
                            },
                            "required": ["text", "expires_at"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "topic",
                    "open_loops",
                    "pending_commitments",
                    "short_term_facts",
                ],
                "additionalProperties": False,
            },
            "mood": MOOD_DECISION_SCHEMA,
        },
        "required": [
            "delivery",
            "messages",
            "expects_reply",
            "reply_expectation",
            "continuity",
            "mood",
        ],
        "additionalProperties": False,
    },
}

SEND_MESSAGE_TOOL_SPEC: dict[str, Any] = {
    "name": "send_message",
    "description": (
        "A live conversational beat with the owner that does not end the Turn. During "
        "substantial multi-step work, actively use it at a natural turning point such "
        "as an unexpected error or retry, a changed plan, a meaningful discovery, or "
        "a real delay. React briefly in Momoi's personal voice instead of writing a "
        "status report. Skip routine steps and never repeat the final respond."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "delivery": DELIVERY_SCHEMA,
            "messages": {
                "type": "array",
                "minItems": 1,
                "items": CHANNEL_MESSAGE_SCHEMA,
            },
        },
        "required": ["delivery", "messages"],
        "additionalProperties": False,
    },
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


HEARTBEAT_FINISH_SPEC: dict[str, Any] = {
    "name": "heartbeat_finish",
    "description": (
        "Required terminal decision for a cognitive heartbeat. It atomically updates "
        "Momoi's activity, optional mood, next heartbeat, and optional owner messages."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "maxItems": 3,
                "items": CHANNEL_MESSAGE_SCHEMA,
            },
            "expects_reply": {
                "type": "boolean",
                "description": (
                    "Whether Momoi, guided by her Soul, relationship with the owner, "
                    "and current context, will genuinely keep attention on a reply to "
                    "these messages."
                ),
            },
            "reply_expectation": {
                "type": "string",
                "maxLength": 300,
                "description": (
                    "What Momoi is waiting for when expects_reply is true; otherwise "
                    "empty."
                ),
            },
            "continue_waiting_for_reply": {
                "type": "boolean",
                "description": (
                    "When pending_owner_reply exists, whether Momoi still genuinely "
                    "wants the runtime to keep checking for that reply. False releases "
                    "the waiting thread; use false when no reply is pending."
                ),
            },
            "activity": {"type": "string", "minLength": 1, "maxLength": 300},
            "result": {"type": "string", "maxLength": 2000},
            "next_check_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1440,
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "mood": MOOD_DECISION_SCHEMA,
        },
        "required": [
            "messages",
            "expects_reply",
            "reply_expectation",
            "continue_waiting_for_reply",
            "activity",
            "result",
            "next_check_minutes",
            "reason",
            "mood",
        ],
        "additionalProperties": False,
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
        "reflection record and promotes only durable, evidence-backed learning."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 6000},
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
