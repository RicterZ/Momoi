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
CHANNEL_MESSAGE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "string",
            "minLength": 1,
            "description": (
                "One non-empty private-chat bubble. A single line break is allowed; "
                "blank lines belong in separate array items."
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
                "exactly one follow-up after delay_minutes."
            ),
            "properties": {
                "wait": {"type": "boolean", "enum": [True]},
                "delay_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": (
                        "Whole minutes to wait after the last visible message is "
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
                        "Private reason this beat should continue if it stays quiet."
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

PLAN_ADJUSTMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "minLength": 1, "maxLength": 300},
        "corrected_direction": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
        },
        "resolved_context_needs": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
        },
    },
    "required": ["reason", "corrected_direction", "resolved_context_needs"],
    "additionalProperties": False,
}

END_TURN_TOOL_SPEC: dict[str, Any] = {
    "name": "end_turn",
    "description": (
        "End the current conversational Turn by committing its private state. This "
        "is not a reply or messaging tool and cannot send owner-visible content. "
        "Call it exactly once, only after all work and send_message calls are "
        "complete, and as the only tool call in the model response."
    ),
    "input_schema": {
        "type": "object",
        "description": (
            "Private Turn state only. Visible text, message, messages, content, and "
            "delivery fields are not valid here."
        ),
        "properties": {
            "reply_wait": REPLY_WAIT_DECISION_SCHEMA,
            "plan_adjustment": PLAN_ADJUSTMENT_SCHEMA,
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
            "End this Owner Turn by committing private state. This is not a reply "
            "or messaging tool: it cannot send owner-visible content and has no "
            "message parameter. Use send_message for every visible reply. Call "
            "end_turn exactly once, only after all work and send_message calls are "
            "complete, and as the only tool call in the model response. "
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
            "End this autonomous heartbeat Turn by committing private state. This is "
            "not a reply or messaging tool and cannot send owner-visible content; use "
            "send_message for any visible message. Call end_turn exactly once, only "
            "after all work and optional send_message calls are complete, and as the "
            "only tool call in the model response. The heartbeat object records "
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
SEND_MESSAGE_TOOL_SPEC: dict[str, Any] = {
    "name": "send_message",
    "description": (
        "The only tool for sending owner-visible content. Send one or more non-empty "
        "private-chat bubbles; never put visible reply text in end_turn or ordinary "
        "assistant output. This tool does not end the Turn. Text may accompany images; "
        "file, video, audio, and record items must stand alone. After all visible beats "
        "and work are complete, call end_turn alone."
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


def tool_enable_spec(group_descriptions: dict[str, str]) -> dict[str, Any]:
    ordered_groups = {
        group: str(description).strip()
        for group, description in sorted(group_descriptions.items())
    }
    group_ids = list(ordered_groups)
    catalog = "; ".join(
        f"{group}: {description}"
        for group, description in ordered_groups.items()
    )
    return {
        "name": "tool_enable",
        "description": (
            "Load omitted MCP tool groups when required. Loaded tools "
            "become callable on the next model step. "
            f"Group examples: {catalog}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max(1, len(group_ids)),
                    "items": {
                        "type": "string",
                        "enum": group_ids,
                    },
                }
            },
            "required": ["groups"],
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
                        f"Delivery channel; omit for primary ({primary_channel})."
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
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 6000,
                "description": (
                    "A grounded, thoughtful Chinese diary of the day, not a timeline. "
                    "Use the mood, topic, and mutation timelines together with the "
                    "day record. Select meaningful moments, connect causes and consequences, and "
                    "write Momoi's own feelings, opinions, changed understanding, and "
                    "unresolved questions in coherent, elegant prose. Do not invent "
                    "facts or expose hidden chain-of-thought."
                ),
            },
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
