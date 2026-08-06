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
        "state": {"type": "string", "enum": sorted(MOOD_STATES)},
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
        "Required terminal decision for every conversational Turn, called only after "
        "all tool work is complete and always as the only tool call in its response. "
        "It may add one final beat or carry no message when send_message already "
        "completed the visible stream or silence is natural."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "items": CHANNEL_MESSAGE_SCHEMA,
            },
            "expects_reply": {
                "type": "boolean",
                "description": (
                    "Whether Momoi will genuinely keep attention on the owner's reply "
                    "to the last visible message in this whole Turn, including an "
                    "earlier send_message when messages is empty."
                ),
            },
            "reply_expectation": {
                "type": "string",
                "maxLength": 300,
                "description": (
                    "What Momoi is waiting for after the Turn's last visible message "
                    "when expects_reply is true; otherwise use an empty string."
                ),
            },
            "mood": MOOD_DECISION_SCHEMA,
        },
        "required": [
            "messages",
            "expects_reply",
            "reply_expectation",
            "mood",
        ],
        "additionalProperties": False,
    },
}

HEARTBEAT_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
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
    },
    "required": [
        "continue_waiting_for_reply",
        "activity",
        "result",
        "next_check_minutes",
        "reason",
    ],
    "additionalProperties": False,
}


def heartbeat_respond_tool_spec() -> dict[str, Any]:
    schema = RESPOND_TOOL_SPEC["input_schema"]
    return {
        **RESPOND_TOOL_SPEC,
        "description": (
            "Required terminal decision for this autonomous heartbeat Turn, called "
            "only after all tool work and optional send_message calls are complete. "
            "The heartbeat object records Momoi's activity and schedules her next Turn."
        ),
        "input_schema": {
            **schema,
            "properties": {
                **schema["properties"],
                "messages": {
                    **schema["properties"]["messages"],
                    "maxItems": 3,
                },
                "heartbeat": HEARTBEAT_STATE_SCHEMA,
            },
            "required": [*schema["required"], "heartbeat"],
        },
    }


SEND_MESSAGE_TOOL_SPEC: dict[str, Any] = {
    "name": "send_message",
    "description": (
        "Emit one or more owner-visible conversational beats now without ending the "
        "Turn. Use it for an immediate genuine conversational reaction or a meaningful live "
        "task update, not routine status. A pre-work update is optional and should be "
        "one brief sentence about the goal or expected wait, never a narration of "
        "internal workflow. factual claims must wait for evidence. After its result "
        "is observed, finish the Turn with an empty respond when the visible stream "
        "is complete; never repeat its content."
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
