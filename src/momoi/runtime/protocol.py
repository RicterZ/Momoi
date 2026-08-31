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
                "One non-empty private-chat bubble; put blank lines in separate "
                "items. With <emotion_catalog>, exact emotion://<listed-slug> sends "
                "that standalone reaction image."
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
        "message. Use wait=false when it is complete, nothing remains open, or "
        "another scheduler owns the work. Use wait=true when a reply, reaction, "
        "information still coming, or a later continuation is genuinely expected; "
        "do not default to false merely because the close feels routine. wait=true "
        "requires a visible message in this Turn and schedules exactly one "
        "follow-up if the owner stays silent."
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
                        "Concrete direction for the one follow-up Momoi should add "
                        "after her last sent message if the owner stays quiet; describe "
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

PLAN_ADJUSTMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Include only when current owner intent or verified tool evidence "
        "materially overturns the Planner handoff. Omit it when the handoff was "
        "adequate; do not manufacture feedback merely because this field exists."
    ),
    "properties": {
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": "Why the supplied handoff was materially wrong.",
        },
        "corrected_direction": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "The corrected execution or delivery direction.",
        },
        "resolved_context_needs": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
            "description": (
                "Planner context needs that current evidence or tool results "
                "resolved despite the adjustment."
            ),
        },
    },
    "required": ["reason", "corrected_direction", "resolved_context_needs"],
    "additionalProperties": False,
}

END_TURN_TOOL_SPEC: dict[str, Any] = {
    "name": "end_turn",
    "description": (
        "Terminal action that commits private conversational Turn state; it cannot "
        "send owner-visible content. Call it exactly once and alone after all work "
        "and delivery are complete. After send_message, wait for its result and call "
        "end_turn in a later model response."
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
            "Terminal action for this Owner Turn. It commits private state and "
            "cannot send owner-visible content. Call it exactly once and alone after "
            "all work and delivery are complete. After send_message, wait for its "
            "result and call end_turn in a later model response. "
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
            "state and cannot send owner-visible content. After any send_message "
            "result and completed work, call end_turn exactly once and alone in a "
            "later model response. The heartbeat object records Momoi's activity and "
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
NEW_EPISODE_REF = "new:<slug>"

RECALL_TOOL_SPEC: dict[str, Any] = {
    "name": "recall",
    "description": (
        "Mandatory first action of every Owner Turn. Submit the minimum complete "
        "historical scope for each independent intent and its Episode disposition. "
        "The runtime returns confirmed memory, dated reflection and Episode "
        "summaries. This call selects context; it does not answer the owner or "
        "perform the requested work."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "units": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "maxLength": 160,
                            "description": (
                                "Brief operative outcome for this independent unit. "
                                "Fold corrections into the final intended outcome."
                            ),
                        },
                        "recall_mode": {
                            "type": "string",
                            "enum": ["search", "reuse"],
                            "description": (
                                "search for a new or changed historical scope; reuse "
                                "only a displayed prior scope that already covers this "
                                "one completely."
                            ),
                        },
                        "recall_queries": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": 3,
                            "description": (
                                "Required and non-empty for search; empty for reuse. "
                                "Use the fewest non-overlapping evidence needs."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "semantic": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 240,
                                        "description": (
                                            "One self-contained declarative retrieval "
                                            "need using canonical subjects supported by "
                                            "the conversation. If an identity remains "
                                            "unresolved, describe that missing referent "
                                            "without assigning a guessed identity. Name "
                                            "the missing fact, "
                                            "relationship, convention, preference, prior "
                                            "interaction or task state. Do not write a "
                                            "question, copy conversational wording, or "
                                            "include a guessed answer."
                                        ),
                                    },
                                    "keywords": {
                                        "type": "array",
                                        "minItems": 0,
                                        "maxItems": 6,
                                        "items": {"type": "string", "maxLength": 60},
                                        "description": (
                                            "Independent sparse OR anchors: literal "
                                            "canonical names, identifiers, titles or "
                                            "exact phrases only. Omit verbs, pronouns, "
                                            "generic words and inferred answer terms; "
                                            "leave empty when no reliable anchor exists."
                                        ),
                                    },
                                },
                                "required": ["semantic"],
                                "additionalProperties": False,
                            },
                        },
                        "recall_from_turn_id": {
                            "type": "string",
                            "description": (
                                "Required for reuse and empty for search. Must be a "
                                "Turn shown in recent_recall_context."
                            ),
                        },
                        "episode": {
                            "type": "object",
                            "description": (
                                "Independent archival decision for this unit; it never "
                                "changes whether recall is search or reuse."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["none", "continue", "new"],
                                    "description": (
                                        "continue only the same concrete experience; "
                                        "new only when this already forms a retainable "
                                        "experience; otherwise none."
                                    ),
                                },
                                "ref": {
                                    "type": "string",
                                    "description": (
                                        "Existing candidate Episode id for continue; "
                                        f"{NEW_EPISODE_REF} chosen for new; empty for "
                                        "none."
                                    ),
                                },
                                "title": {
                                    "type": "string",
                                    "maxLength": 80,
                                    "description": (
                                        "Specific experience title for new; empty "
                                        "otherwise."
                                    ),
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    "required": [
                        "intent",
                        "recall_mode",
                        "recall_queries",
                        "recall_from_turn_id",
                        "episode",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["units"],
        "additionalProperties": False,
    },
}

SEND_MESSAGE_TOOL_SPEC: dict[str, Any] = {
    "name": "send_message",
    "description": (
        "Use for owner-visible delivery, independently of work. Put each non-empty "
        "chat bubble in messages. Ordinary assistant content is discarded. After "
        "its result and work, call end_turn alone in a later response. Text may "
        "accompany images; file, video, audio, and record items must stand alone."
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

READ_TOOL_RESULT_SPEC: dict[str, Any] = {
    "name": "read_tool_result",
    "description": (
        "Continue reading an exact private snapshot when a tool result returned "
        "truncated=true, result_ref, and next_cursor. Pass result_ref unchanged "
        "and the latest next_cursor; omit cursor only for the first chunk. This "
        "does not call the original tool again and cannot read workspace files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "string",
                "pattern": "^tr_[0-9a-f]{32}$",
                "description": "Opaque result_ref returned by a truncated tool result.",
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": "Opaque next_cursor from the preceding chunk.",
            },
        },
        "required": ["result_ref"],
        "additionalProperties": False,
    },
}

REFLECTION_FINISH_SPEC: dict[str, Any] = {
    "name": "reflection_finish",
    "description": (
        "Required terminal result for the private daily retrospective. It stores the "
        "reflection record, promotes only durable evidence-backed learning, and "
        "optionally closes finished conversations. Confirmed owner memory is read-only "
        "in Reflection and is maintained by a separate runtime stage."
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
                            "description": (
                                "Use tool_skill for reusable knowledge about a "
                                "specific tool or integration. Use practice for a "
                                "reusable method, workflow, or decision process whose "
                                "main lesson is not a particular tool invocation."
                            ),
                        },
                        "key": {
                            "type": "string",
                            "description": "Stable lowercase dot-separated key.",
                        },
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Concise reusable lesson, including when it applies "
                                "and how to verify success or avoid a known failure."
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "Exact contiguous quote from the supplied day or tool "
                                "evidence."
                            ),
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
