import copy
import json
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
        "JSON object, never a mood name string. Use {\"decision\": \"unchanged\"} only "
        "when the persistent mood remains accurate; unchanged permits ONLY decision, so omit "
        "state, intensity and cause entirely. For updated, supply decision, state, intensity, cause. "
        "Reassess the persistent mood from its age and this Turn. Update when state, "
        "intensity, or continuing cause changes, including natural settling. Keep it "
        "only while all three remain accurate; ignore a reaction that is truly momentary."
    ),
    "oneOf": [
        {
            "type": "object",
            "title": "Keep the existing mood without resubmitting it",
            "description": 'Exactly {"decision":"unchanged"}. Do not copy current state, intensity or cause here.',
            "examples": [{"decision": "unchanged"}],
            "properties": {"decision": {"type": "string", "enum": ["unchanged"]}},
            "required": ["decision"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "title": "Update the persistent mood",
            "description": "Set decision=updated and supply all three update fields.",
            "examples": [{"decision": "updated", "state": "calm", "intensity": 0.3,
                          "cause": "事情已处理完，心情平静下来"}],
            "properties": {
                "decision": {"type": "string", "enum": ["updated"]},
                **MOOD_UPDATE_SCHEMA["properties"],
            },
            "required": ["decision", *MOOD_UPDATE_SCHEMA["required"]],
            "additionalProperties": False,
        },
    ],
}

REPLY_WAIT_DECISION_SCHEMA: dict[str, Any] = {
    "description": (
        "JSON object, never a bare boolean: {\"wait\": false} when complete. "
        "Whether the last visible bubble leaves a real open beat. false when complete "
        "or another scheduler owns the work. true only while awaiting a reply, "
        "reaction, incoming information, or the assistant's later continuation; it requires "
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

HEARTBEAT_ACTIVITY_TOOL_SPEC: dict[str, Any] = {
    "name": "heartbeat_activity",
    "description": (
        "Record this Heartbeat's actual activity or rest and its result. "
        "Visible in every conversation workflow, callable only during Heartbeat. "
        "Must succeed before end_turn, in an earlier round. "
        "Stages the latest report for atomic commit when the Heartbeat completes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "activity": {
                "type": "string", "minLength": 1, "maxLength": 300,
                "description": "Actual activity or rest during this Heartbeat.",
            },
            "result": {
                "type": "string", "maxLength": 2000,
                "description": "Concrete outcome; empty when none.",
            },
        },
        "required": ["activity", "result"],
        "additionalProperties": False,
    },
}

HEARTBEAT_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Required ONLY in the current heartbeat workflow. Omit the entire heartbeat "
        "field in owner, webhook, reply_followup and goal Turns, even when runtime_state "
        "shows a previous heartbeat or its schedule. Historical state is not an instruction "
        "to write this field. Use the configured interval bounds from the current workflow."
    ),
    "properties": {
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
        "next_check_minutes",
        "reason",
    ],
    "additionalProperties": False,
}

END_TURN_EXAMPLE = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}

END_TURN_TOOL_SPEC: dict[str, Any] = {
    "name": "end_turn",
    "description": (
        "Commit private state and finish this Turn. Does not send a message. "
        "Call alone or last after send_bubbles/send_voice in the same response; "
        "delivery must succeed first. Other work tools must finish in earlier rounds. "
        "reply_wait and mood are objects, not false or a mood-name string. "
        "Shared contract: owner/webhook require reply_wait and mood; heartbeat also requires "
        "heartbeat and an earlier successful heartbeat_activity; reply_followup requires "
        "reply_wait.wait=false; goal accepts only goal. Runtime enforces stage permissions "
        "and configured heartbeat intervals. Do not write [tool_call] text or invent execution results. "
        "Conversation example (adapt to actual state): "
        + json.dumps(END_TURN_EXAMPLE, ensure_ascii=False)
    ),
    "input_schema": {
        "type": "object",
        "examples": [
            copy.deepcopy(END_TURN_EXAMPLE),
            {"reply_wait": {"wait": False}, "mood": {
                "decision": "updated", "state": "calm", "intensity": 0.3,
                "cause": "事情已处理完，心情平静下来"}},
            {"reply_wait": {"wait": True, "delay_minutes": 3,
                            "expected_information": "主人选择的见面时间",
                            "reason": "若未回复，补充可选时间以便安排"},
             "mood": {"decision": "unchanged"}},
            {**copy.deepcopy(END_TURN_EXAMPLE), "heartbeat": {
                "next_check_minutes": 30, "reason": "当前无待处理事项，稍后检查"}},
            {"goal": {"status": "done", "result": "已完成检查并验证结果"}},
        ],
        "properties": {
            "reply_wait": REPLY_WAIT_DECISION_SCHEMA,
            "mood": MOOD_DECISION_SCHEMA,
            "heartbeat": HEARTBEAT_STATE_SCHEMA,
            "goal": {"default": None, "oneOf": [{"type": "null"}, GOAL_REVIEW_SCHEMA]},
        },
        "oneOf": [
            {
                "title": "Owner, webhook or reply follow-up completion",
                "description": "Omit heartbeat. reply_followup additionally requires reply_wait.wait=false.",
                "examples": [copy.deepcopy(END_TURN_EXAMPLE)],
                "required": ["reply_wait", "mood"],
                "properties": {"goal": {"type": "null"}, "heartbeat": False},
            },
            {
                "title": "Goal completion only",
                "description": "Only in the goal workflow; submit only goal. Omit reply_wait, mood and heartbeat.",
                "examples": [{"goal": {"status": "done", "result": "已完成检查并验证结果"}}],
                "required": ["goal"],
                "properties": {
                    "goal": {"type": "object"},
                    "reply_wait": False,
                    "mood": False,
                    "heartbeat": False,
                },
            },
            {
                "title": "Heartbeat completion only",
                "description": "Only in the heartbeat workflow after heartbeat_activity succeeds. The example interval must be adapted to configured bounds.",
                "examples": [{**copy.deepcopy(END_TURN_EXAMPLE), "heartbeat": {
                    "next_check_minutes": 30, "reason": "当前无待处理事项，稍后检查"}}],
                "required": ["reply_wait", "mood", "heartbeat"],
                "properties": {"goal": {"type": "null"}},
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
    """Build a private error contract, never a provider-visible tool projection.

    All conversation stages send END_TURN_TOOL_SPEC unchanged. This projection
    only supplies stage-specific missing fields and examples in error results.
    """
    spec = copy.deepcopy(END_TURN_TOOL_SPEC)
    schema = spec["input_schema"]
    schema.pop("oneOf")
    properties = schema["properties"]
    if stage == "goal":
        schema["properties"] = {"goal": copy.deepcopy(GOAL_REVIEW_SCHEMA)}
        schema["required"] = ["goal"]
    else:
        required = ["reply_wait", "mood"]
        if stage == "heartbeat":
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
        elif stage not in {"webhook", "owner"}:
            raise ValueError(f"end_turn is not available in {stage}")
        schema["required"] = required
        schema["properties"] = {key: properties[key] for key in required}
        schema["properties"]["goal"] = {"type": "null"}
    example = copy.deepcopy(END_TURN_EXAMPLE)
    if stage == "goal":
        example = {"goal": {"status": "done", "result": "已完成检查并验证结果"}}
    elif stage == "heartbeat":
        example["heartbeat"] = {
            "next_check_minutes": interval["minimum"],
            "reason": "当前无待处理事项，稍后检查",
        }
    schema["examples"] = [example]
    if stage != "goal":
        updated = copy.deepcopy(example)
        updated["mood"] = {"decision": "updated", "state": "calm", "intensity": 0.3, "cause": "事情已处理完，心情平静下来"}
        schema["examples"].append(updated)
        if stage != "reply_followup":
            waiting = copy.deepcopy(example)
            waiting["reply_wait"] = {
                "wait": True, "delay_minutes": 3,
                "expected_information": "主人选择的见面时间",
                "reason": "若未回复，补充可选时间以便安排",
            }
            schema["examples"].append(waiting)
    return spec


def end_turn_correction(error: str, schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    hints = {
        "end_turn_must_be_alone": "Call end_turn alone, or last after send_bubbles/send_voice. Finish all other tools in earlier rounds.",
        "send_bubbles_required_before_end_turn": "Plain assistant text is not delivery. Send it with send_bubbles/send_voice before end_turn.",
        "goal_required_in_end_turn": "This is a Goal Turn: supply only goal with its actual status and result; follow the status-specific required fields.",
        "goal_end_turn_only_accepts_goal": "Remove all fields except goal in this Goal Turn.",
        "goal_not_allowed_in_end_turn": "This is not a Goal Turn; omit goal and supply reply_wait and mood objects.",
        "heartbeat_activity_required_before_end_turn": "Call heartbeat_activity successfully in an earlier round, then retry end_turn.",
        "invalid_mood_decision": 'mood must be {"decision":"unchanged"} or {"decision":"updated","state":"calm","intensity":0.3,"cause":"具体原因"}. A string is invalid.',
        "invalid_reply_wait_decision": f'reply_wait must be {{"wait":false}} or an object with wait=true, delay_minutes (integer {REPLY_WAIT_MIN_MINUTES}-{REPLY_WAIT_MAX_MINUTES}), expected_information and reason. A boolean is invalid; wait=false accepts no other fields.',
        "reply_expectation_without_visible_bubble": "Send the actual question/continuation with send_bubbles or send_voice before waiting. Use wait=false if the conversation is complete.",
        "reply_followup_cannot_schedule_another_wait": 'This follow-up cannot schedule another follow-up; use reply_wait={"wait":false}.',
        "reply_followup_bubble_required": "Deliver this follow-up with send_bubbles or send_voice before ending.",
        "bubbles_not_allowed_in_end_turn": "Send bubbles through send_bubbles first; remove bubbles from end_turn.",
        "heartbeat_state_not_allowed": "Remove heartbeat; it is only valid in a Heartbeat Turn.",
        "activity_not_allowed_in_end_turn": "Remove activity; record Heartbeat activity through heartbeat_activity before ending.",
        "legacy_reply_wait_fields_not_allowed": "Remove expects_reply, reply_expectation and schedule_reply_wait; use the reply_wait object.",
    }
    missing = [key for key in schema.get("required", []) if key not in arguments]
    message = hints.get(error, "Correct the arguments to match this stage's schema and retry end_turn as a native tool call.")
    field_errors = []
    if error == "heartbeat_state_not_allowed":
        field_errors.append({"path": "$.heartbeat", "issue": "forbidden_in_current_workflow", "expected": "field omitted"})
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
    if error in {"heartbeat_interval_out_of_range", "invalid_heartbeat_state"}:
        interval = schema["properties"]["heartbeat"]["properties"]["next_check_minutes"]
        message = f'heartbeat requires next_check_minutes (integer {interval["minimum"]}-{interval["maximum"]}) and a nonempty reason (at most 500 characters).'
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
