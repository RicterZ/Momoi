import copy
import math
from typing import Any

from ...tools.contracts.agenda import GOAL_REVIEW_SCHEMA
from ...reply_wait import REPLY_WAIT_MAX_MINUTES, REPLY_WAIT_MIN_MINUTES

SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "pattern": "^[a-zA-Z0-9_-]{1,40}$",
            "description": "Segment type, e.g. text, image, file, video, audio, record, reply, link, location, or mention.",
        },
        "data": {
            "type": "object",
            "description": (
                "Payload: text uses text; reply uses id; media uses file with a "
                "local path, HTTP(S) URL, or base64 resource."
            ),
        },
    },
    "required": ["type", "data"],
    "additionalProperties": False,
}

CHANNEL_BUBBLE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "string",
            "minLength": 1,
            "description": (
                "Put "
                "blank-line-separated text in separate bubbles. An emotion:// value "
                "must exactly match emotion://<listed-slug> from <emotion_catalog>; "
                "it sends a standalone reaction image."
            ),
        },
        {
            "type": "object",
            "description": "Text may accompany images; file, video, audio, and record messages must stand alone.",
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
            "description": ("Current mood, e.g. curious, calm, frustrated, or tired."),
        },
        "intensity": {"type": "number", "minimum": 0, "maximum": 1},
        "cause": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["state", "intensity", "cause"],
    "additionalProperties": False,
}

MOOD_DECISION_SCHEMA: dict[str, Any] = {
    "description": (
        "Reassess the persistent mood from its age and this Turn. Update when state, "
        "intensity, or continuing cause changes, including natural settling. Keep it "
        "only while all three remain accurate; ignore a reaction that is truly momentary."
    ),
    "oneOf": [
        {
            "type": "object",
            "properties": {"decision": {"type": "string", "enum": ["unchanged"]}},
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
    ],
}

ACTIVITY_DECISION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "description": "Keep the current activity text and result unchanged.",
            "properties": {"decision": {"type": "string", "enum": ["unchanged"]}},
            "required": ["decision"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "description": "Replace a contradicted activity text or result.",
            "properties": {
                "decision": {"type": "string", "enum": ["updated"]},
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "Concise corrected Current self state activity.",
                },
                "result": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "Corrected outcome; empty if none is now true.",
                },
            },
            "required": ["decision", "text", "result"],
            "additionalProperties": False,
        },
    ]
}

REPLY_WAIT_DECISION_SCHEMA: dict[str, Any] = {
    "description": (
        "Whether the last visible bubble leaves a real open beat. false when complete "
        "or another scheduler owns the work. true only while awaiting a reply, "
        "reaction, incoming information, or Momoi's later continuation; it requires "
        "a visible bubble and schedules one follow-up Turn after silence."
    ),
    "oneOf": [
        {
            "type": "object",
            "description": "Complete; no follow-up for this beat.",
            "properties": {
                "wait": {"type": "boolean", "enum": [False]},
            },
            "required": ["wait"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "description": "Open; after delay_minutes of silence, run one follow-up Turn.",
            "properties": {
                "wait": {"type": "boolean", "enum": [True]},
                "delay_minutes": {
                    "type": "integer",
                    "minimum": REPLY_WAIT_MIN_MINUTES,
                    "maximum": REPLY_WAIT_MAX_MINUTES,
                    "description": "Whole minutes after successful bubble delivery.",
                },
                "expected_information": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": (
                        "The reply, reaction, incoming information, or Momoi "
                        "continuation that would complete this beat."
                    ),
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": (
                        "Concrete new conversational move for the one silent-owner "
                        "follow-up; do not merely restate what is awaited."
                    ),
                },
            },
            "required": [
                "wait",
                "delay_minutes",
                "expected_information",
                "reason",
            ],
            "additionalProperties": False,
        },
    ],
}

HEARTBEAT_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "activity": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": "Actual activity or rest during this Heartbeat.",
        },
        "result": {
            "type": "string",
            "maxLength": 2000,
            "description": "Concrete outcome; empty when none.",
        },
        "next_check_minutes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1440,
            "description": "Delay until the next autonomous check.",
        },
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "Why this activity and next check fit the current situation.",
        },
    },
    "required": [
        "activity",
        "result",
        "next_check_minutes",
        "reason",
    ],
    "additionalProperties": False,
}

END_TURN_TOOL_SPEC: dict[str, Any] = {
    "name": "end_turn",
    "description": (
        "Commit private state and finish this Turn. Does not send a message. "
        "Call alone or last after send_bubbles/send_voice in the same response; "
        "delivery must succeed first. Other work tools must finish in earlier rounds."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reply_wait": REPLY_WAIT_DECISION_SCHEMA,
            "mood": MOOD_DECISION_SCHEMA,
            "activity": ACTIVITY_DECISION_SCHEMA,
            "heartbeat": HEARTBEAT_STATE_SCHEMA,
            "goal": {"default": None, "oneOf": [{"type": "null"}, GOAL_REVIEW_SCHEMA]},
        },
        "oneOf": [
            {
                "required": ["reply_wait", "mood"],
                "properties": {"goal": {"type": "null"}},
            },
            {
                "required": ["goal"],
                "properties": {
                    "goal": {"type": "object"},
                    "reply_wait": False,
                    "mood": False,
                    "activity": False,
                    "heartbeat": False,
                },
            },
        ],
        "additionalProperties": False,
    },
}


def end_turn_tool_spec(
    stage: str,
    *,
    heartbeat_min_interval_seconds: int = 60,
    heartbeat_max_interval_seconds: int = 86400,
) -> dict[str, Any]:
    """Project the catalog template onto the executing Turn's actual contract."""
    spec = copy.deepcopy(END_TURN_TOOL_SPEC)
    schema = spec["input_schema"]
    schema.pop("oneOf")
    properties = schema["properties"]
    if stage == "goal":
        schema["properties"] = {"goal": copy.deepcopy(GOAL_REVIEW_SCHEMA)}
        schema["required"] = ["goal"]
    else:
        required = ["reply_wait", "mood"]
        if stage == "owner":
            required.append("activity")
        elif stage == "heartbeat":
            required.append("heartbeat")
            interval = properties["heartbeat"]["properties"]["next_check_minutes"]
            interval["minimum"] = max(1, math.ceil(heartbeat_min_interval_seconds / 60))
            interval["maximum"] = min(
                1440, math.floor(heartbeat_max_interval_seconds / 60)
            )
        elif stage == "reply_followup":
            properties["reply_wait"] = copy.deepcopy(
                REPLY_WAIT_DECISION_SCHEMA["oneOf"][0]
            )
        elif stage != "webhook":
            raise ValueError(f"end_turn is not available in {stage}")
        schema["required"] = required
        schema["properties"] = {key: properties[key] for key in required}
        schema["properties"]["goal"] = {"type": "null"}
    return spec


SEND_BUBBLES_TOOL_SPEC: dict[str, Any] = {
    "name": "send_bubbles",
    "description": (
        "Send messages to the owner. Starts delivery immediately, independently of end_turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bubbles": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Ordered owner-visible messages; each item is delivered as one "
                    "separate chat bubble."
                ),
                "items": CHANNEL_BUBBLE_SCHEMA,
            },
        },
        "required": ["bubbles"],
        "additionalProperties": False,
    },
}


def send_bubbles_tool_spec(channel_names: list[str]) -> dict[str, Any]:
    return {
        **SEND_BUBBLES_TOOL_SPEC,
        "input_schema": {
            **SEND_BUBBLES_TOOL_SPEC["input_schema"],
            "properties": {
                **SEND_BUBBLES_TOOL_SPEC["input_schema"]["properties"],
                "channel": {
                    "type": "string",
                    "enum": channel_names,
                    "description": (
                        "Delivery channel; omit to use this Turn's channel."
                    ),
                },
            },
        },
    }
