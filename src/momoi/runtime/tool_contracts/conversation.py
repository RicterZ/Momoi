from typing import Any


SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Channel-neutral type such as text, image, file, video, audio, "
                "reply, link, location, or mention."
            ),
        },
        "data": {
            "type": "object",
            "description": (
                "Segment data: text uses text, reply uses id, and media uses file "
                "with a local path, HTTP(S) URL, or base64 resource."
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
                "A bubble is one item in send_bubbles.bubbles; producing it means "
                "calling send_bubbles. It must be non-empty; split blank lines into "
                "items. Exact emotion://<listed-slug> from <emotion_catalog> sends "
                "a standalone reaction image."
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
    "description": (
        "Persistent but evolving mood state. Reassess the Current self state mood "
        "at every end_turn using its age and this Turn's emotional context. Update "
        "when the state meaningfully shifts, intensity changes, the recorded cause "
        "no longer has continuing influence, or the mood naturally settles as time "
        "and context move on. Keep it unchanged only while its existing state, "
        "intensity, and cause all remain accurate. A truly momentary reaction that "
        "leaves the underlying mood intact does not by itself require an update; do "
        "not otherwise favor unchanged over updated."
    ),
    "oneOf": [
        {
            "type": "object",
            "description": (
                "Keep the existing mood only when its state, intensity, and cause "
                "still accurately describe the current persistent mood."
            ),
            "properties": {
                "decision": {"type": "string", "enum": ["unchanged"]}
            },
            "required": ["decision"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "description": (
                "Replace the mood when its state, intensity, or continuing cause "
                "has changed, including natural settling over time."
            ),
            "properties": {
                "decision": {"type": "string", "enum": ["updated"]},
                **MOOD_UPDATE_SCHEMA["properties"],
            },
            "required": ["decision", *MOOD_UPDATE_SCHEMA["required"]],
            "additionalProperties": False,
        },
    ]
}

ACTIVITY_DECISION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "description": "Keep the current activity text and result unchanged.",
            "properties": {
                "decision": {"type": "string", "enum": ["unchanged"]}
            },
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
                    "description": (
                        "Concise corrected activity snapshot with the same meaning as "
                        "Current self state activity."
                    ),
                },
                "result": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": (
                        "Corrected concrete outcome. It may be empty when no result "
                        "is now true."
                    ),
                },
            },
            "required": ["decision", "text", "result"],
            "additionalProperties": False,
        },
    ]
}

REPLY_WAIT_DECISION_SCHEMA: dict[str, Any] = {
    "description": (
        "Whether this conversational beat remains open after the last visible "
        "bubble. Use wait=false when it is complete, nothing remains open, or "
        "another scheduler owns the work. Use wait=true when a reply, reaction, "
        "information still coming, or a later continuation is genuinely expected; "
        "do not default to false merely because the close feels routine. wait=true "
        "requires a visible bubble in this Turn and schedules one follow-up Turn "
        "if the owner stays silent."
    ),
    "oneOf": [
        {
            "type": "object",
            "description": "The beat is complete; no later follow-up from this remainder.",
            "properties": {
                "wait": {"type": "boolean", "enum": [False]},
            },
            "required": ["wait"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "description": (
                "The beat is still open. If it stays quiet, the runtime sends "
                "one follow-up Turn after delay_minutes."
            ),
            "properties": {
                "wait": {"type": "boolean", "enum": [True]},
                "delay_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": (
                        "Whole minutes to wait after the last visible bubble is "
                        "successfully delivered."
                    ),
                },
                "expected_information": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": (
                        "What would complete this open beat: a reply, a reaction, "
                        "information still coming, or the later continuation Momoi "
                        "intends to make."
                    ),
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": (
                        "Concrete direction for the one follow-up Momoi should add "
                        "after her last sent bubble if the owner stays quiet; describe "
                        "the new conversational move, not merely the reply being awaited."
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
    ]
}

END_TURN_TOOL_SPEC: dict[str, Any] = {
    "name": "end_turn",
    "description": (
        "Terminal action that commits private conversational Turn state; it cannot "
        "send owner-visible content. Call it exactly once and alone after all work "
        "and delivery are complete. After send_bubbles, wait for its result and call "
        "end_turn alone on the next step."
    ),
    "input_schema": {
        "type": "object",
        "description": (
            "Private Turn state only. Visible bubbles and delivery fields are not "
            "valid here."
        ),
        "properties": {
            "reply_wait": REPLY_WAIT_DECISION_SCHEMA,
            "mood": MOOD_DECISION_SCHEMA,
        },
        "required": [
            "reply_wait",
            "mood",
        ],
        "additionalProperties": False,
    },
}

def owner_end_turn_tool_spec() -> dict[str, Any]:
    schema = END_TURN_TOOL_SPEC["input_schema"]
    return {
        **END_TURN_TOOL_SPEC,
        "description": (
            "Terminal action for this Owner Turn. It commits private state and "
            "cannot send owner-visible content. Call it exactly once and alone after "
            "all work and delivery are complete. After send_bubbles, wait for its "
            "result and call end_turn alone on the next step. "
            "For activity, compare Current self state with authenticated owner input "
            "and reliable evidence from this Turn. Correct it only when this Turn "
            "completes, cancels, replaces, proves impossible, invalidates a premise "
            "of the activity, or disproves its result. A different topic, ordinary "
            "conversation, requested work, or a compatible new activity or outcome "
            "is not a conflict. If only the result is wrong, preserve the activity "
            "text; without a conflict, leave both unchanged."
        ),
        "input_schema": {
            **schema,
            "properties": {
                **schema["properties"],
                "activity": ACTIVITY_DECISION_SCHEMA,
            },
            "required": [*schema["required"], "activity"],
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

def heartbeat_end_turn_tool_spec() -> dict[str, Any]:
    schema = END_TURN_TOOL_SPEC["input_schema"]
    return {
        **END_TURN_TOOL_SPEC,
        "description": (
            "Terminal action for this autonomous heartbeat Turn. It commits private "
            "state and cannot send owner-visible content. After any send_bubbles "
            "result and completed work, call end_turn exactly once and alone in a "
            "later step. The heartbeat object records Momoi's activity and "
            "schedules her next Turn."
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

SEND_BUBBLES_TOOL_SPEC: dict[str, Any] = {
    "name": "send_bubbles",
    "description": (
        "Produces owner-visible bubbles; assistant text delivers none. Call only "
        "when warranted. After its result and all work, call end_turn alone on the "
        "next step. Text may "
        "accompany images; file, video, audio, and record items must stand alone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bubbles": {
                "type": "array",
                "minItems": 1,
                "items": CHANNEL_BUBBLE_SCHEMA,
            },
        },
        "required": ["bubbles"],
        "additionalProperties": False,
    },
}

def send_bubbles_tool_spec(
    channel_names: list[str], primary_channel: str
) -> dict[str, Any]:
    return {
        **SEND_BUBBLES_TOOL_SPEC,
        "input_schema": {
            **SEND_BUBBLES_TOOL_SPEC["input_schema"],
            "properties": {
                **SEND_BUBBLES_TOOL_SPEC["input_schema"]["properties"],
                "channel": {
                    "type": "string",
                    "enum": channel_names,
                    "default": primary_channel,
                    "description": (
                        f"Delivery channel; omit for primary ({primary_channel})."
                    ),
                },
            },
        },
    }

