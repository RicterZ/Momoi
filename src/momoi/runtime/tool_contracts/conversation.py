import copy
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
    "type": "object",
    "description": (
        "JSON object, never a mood name string. Use {\"decision\": \"unchanged\"} only "
        "when the persistent mood remains accurate; unchanged permits ONLY decision, so omit "
        "state, intensity and cause entirely. For updated, supply decision, state, intensity, cause. "
        "Reassess the persistent mood from its age and this Turn. Update when state, "
        "intensity, or continuing cause changes, including natural settling. Keep it "
        "only while all three remain accurate; ignore a reaction that is truly momentary."
    ),
    "properties": {
        "decision": {"type": "string", "enum": ["unchanged", "updated"]},
        **MOOD_UPDATE_SCHEMA["properties"],
    },
    "required": ["decision"],
    "additionalProperties": False,
    "examples": [
        {"decision": "unchanged"},
        {"decision": "updated", "state": "calm", "intensity": 0.3,
         "cause": "The task is complete and I feel settled."},
    ],
    "oneOf": [
        {
            "properties": {"decision": {"enum": ["unchanged"]}},
            "maxProperties": 1,
        },
        {
            "properties": {"decision": {"enum": ["updated"]}},
            "required": list(MOOD_UPDATE_SCHEMA["required"]),
        },
    ],
}

REPLY_WAIT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "JSON object, never a bare boolean or JSON-encoded string. "
        "When wait=false, send only {\"wait\":false}; omit all other fields. "
        "When wait=true, include delay_minutes, expected_information and reason. "
        "Whether the last visible bubble leaves a real open beat. false when complete "
        "or another scheduler owns the work. true only while awaiting a reply, "
        "reaction, incoming information, or the assistant's later continuation; it requires "
        "a visible bubble and schedules one follow-up Turn after silence."
    ),
    "properties": {
        "wait": {"type": "boolean"},
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
                "The reply, reaction, incoming information, or assistant "
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
    "required": ["wait"],
    "additionalProperties": False,
    "examples": [{"wait": False}],
    "oneOf": [
        {
            "properties": {"wait": {"enum": [False]}},
            "maxProperties": 1,
        },
        {
            "properties": {"wait": {"enum": [True]}},
            "required": ["delay_minutes", "expected_information", "reason"],
        },
    ],
}

HEARTBEAT_ACTIVITY_TOOL_SPEC: dict[str, Any] = {
    "name": "heartbeat_activity",
    "description": (
        "Record this Heartbeat's actual activity or rest, its result and next check schedule. "
        "Visible in every conversation workflow, callable only during Heartbeat. "
        "Must succeed before end_turn, in an earlier round. "
        "Stages the latest report for atomic commit when the Heartbeat completes."
    ),
    "input_schema": {
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
                "description": "Minutes until the next autonomous check; obey the interval bounds in the current Heartbeat context.",
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Why this next check fits the current situation.",
            },
        },
        "required": ["activity", "result", "next_check_minutes", "reason"],
        "additionalProperties": False,
    },
}

GOAL_REVIEW_TOOL_SPEC: dict[str, Any] = {
    "name": "goal_review",
    "description": (
        "Stage the current Goal's result and next action or schedule. Callable only "
        "during a Goal Turn; the runtime supplies its ID. Must succeed in an earlier "
        "round before end_turn({}). Changes commit only when that Turn completes."
    ),
    "input_schema": GOAL_REVIEW_SCHEMA,
}


END_TURN_EXAMPLE = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}

END_TURN_TOOL_SPEC: dict[str, Any] = {
    "name": "end_turn",
    "description": (
        "Finish this Turn and commit its staged state. Does not send a message. "
        "Call alone or last after successful send_bubbles/send_voice; other work tools "
        "must finish in earlier rounds. For owner, webhook, heartbeat and reply_followup, "
        "supply mood and reply_wait objects; reply_followup requires wait=false. "
        "Heartbeat requires an earlier successful heartbeat_activity. For Goal, call "
        "goal_review successfully first, then end_turn with empty arguments {}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reply_wait": REPLY_WAIT_DECISION_SCHEMA,
            "mood": MOOD_DECISION_SCHEMA,
        },
        "oneOf": [
            {
                "title": "Conversation completion",
                "required": ["reply_wait", "mood"],
                "examples": [copy.deepcopy(END_TURN_EXAMPLE)],
            },
            {
                "title": "Goal completion after goal_review",
                "maxProperties": 0,
                "examples": [{}],
            },
        ],
        "examples": [copy.deepcopy(END_TURN_EXAMPLE), {}],
        "additionalProperties": False,
    },
}


def end_turn_tool_spec(stage: str) -> dict[str, Any]:
    """Stage-specific error guidance; provider-visible schemas remain unchanged."""
    spec = copy.deepcopy(END_TURN_TOOL_SPEC)
    schema = spec["input_schema"]
    schema.pop("oneOf")
    if stage == "goal":
        schema["properties"] = {}
        schema["required"] = []
        schema["examples"] = [{}]
    elif stage in {"owner", "heartbeat", "webhook", "reply_followup"}:
        schema["required"] = ["reply_wait", "mood"]
        schema["examples"] = [copy.deepcopy(END_TURN_EXAMPLE)]
        if stage == "reply_followup":
            wait_schema = schema["properties"]["reply_wait"]
            wait_schema.pop("oneOf")
            wait_schema["properties"] = {"wait": {"type": "boolean", "enum": [False]}}
    else:
        raise ValueError(f"end_turn is not available in {stage}")
    return spec


def end_turn_correction(error: str, schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    hints = {
        "end_turn_must_be_alone": "Call end_turn alone, or last after send_bubbles/send_voice. Finish all other tools in earlier rounds.",
        "send_bubbles_required_before_end_turn": "Plain assistant text is not delivery. Send it with send_bubbles/send_voice before end_turn.",
        "goal_review_required_before_end_turn": "Call goal_review successfully in an earlier round, then end_turn({}).",
        "goal_end_turn_requires_empty_arguments": "Submit the Goal outcome through goal_review; end_turn accepts only {} in this stage.",
        "unexpected_end_turn_fields": "end_turn accepts only mood and reply_wait. Submit Goal outcomes through goal_review and Heartbeat activity and schedule through heartbeat_activity.",
        "heartbeat_activity_required_before_end_turn": "Call heartbeat_activity successfully in an earlier round, then retry end_turn.",
        "invalid_mood_decision": 'mood must be {"decision":"unchanged"} or {"decision":"updated","state":"calm","intensity":0.3,"cause":"具体原因"}. A string is invalid.',
        "invalid_reply_wait_decision": f'reply_wait must be {{"wait":false}} or an object with wait=true, delay_minutes (integer {REPLY_WAIT_MIN_MINUTES}-{REPLY_WAIT_MAX_MINUTES}), expected_information and reason. A boolean is invalid; wait=false accepts no other fields.',
        "reply_expectation_without_visible_bubble": "Send the actual question/continuation with send_bubbles or send_voice before waiting. Use wait=false if the conversation is complete.",
        "reply_followup_cannot_schedule_another_wait": 'This follow-up cannot schedule another follow-up; use reply_wait={"wait":false}.',
        "bubbles_not_allowed_in_end_turn": "Send bubbles through send_bubbles first; remove bubbles from end_turn.",
        "activity_not_allowed_in_end_turn": "Remove activity; record Heartbeat activity through heartbeat_activity before ending.",
        "legacy_reply_wait_fields_not_allowed": "Remove expects_reply, reply_expectation and schedule_reply_wait; use the reply_wait object.",
    }
    missing = [key for key in schema.get("required", []) if key not in arguments]
    message = hints.get(error, "Correct the arguments to match this stage's schema and retry end_turn as a native tool call.")
    field_errors = []
    if error == "invalid_mood_decision":
        mood = arguments.get("mood")
        if isinstance(mood, dict) and mood.get("decision") == "unchanged":
            extras = sorted(set(mood) - {"decision"})
            if extras:
                message = (
                    "mood.decision=unchanged permits ONLY decision. Remove "
                    + ", ".join("mood." + key for key in extras)
                    + '. If the mood actually changed, use decision="updated" with state, intensity and cause instead.'
                )
                field_errors.extend({"path": "$.mood." + key, "issue": "forbidden_when_unchanged", "expected": "field omitted"} for key in extras)
        elif isinstance(mood, dict) and mood.get("decision") == "updated":
            absent = [key for key in MOOD_UPDATE_SCHEMA["required"] if key not in mood]
            if absent:
                message = "mood.decision=updated requires: " + ", ".join("mood." + key for key in absent) + "."
                field_errors.extend({"path": "$.mood." + key, "issue": "required_when_updated"} for key in absent)
    for key in missing:
        field_errors.append({"path": "$." + key, "issue": "required_in_current_workflow"})
    if missing:
        message = "Supply all missing fields: " + ", ".join(missing) + ". " + message
    return {
        "field_errors": field_errors,
        "message": message,
        "hint": "The example illustrates structure only. Preserve truthful state decisions and do not resend messages already delivered.",
        "example_arguments": copy.deepcopy(schema["examples"][0]),
    }


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
