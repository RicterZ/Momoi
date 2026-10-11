import copy
from typing import Any

from ...tools.contracts.agenda import GOAL_REVIEW_SCHEMA

SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "pattern": "^[a-zA-Z0-9_-]{1,40}$",
            "description": '分段类型，例如：文本、图像、文件、视频、音频、记录、回复、链接、位置或提及。',
        },
        "data": {
            "type": "object",
            "description": (
                '负载内容：文本使用 text；回复使用 id；媒体使用 file 并附带本地路径、HTTP(S) URL 或 base64 资源。'
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
                '将换行分隔的文本放入独立的气泡中。emotion:// 的值必须精确匹配 <emotion_catalog> 中列出的 emotion://<listed-slug>；它将发送一个独立的反应图像。'
            ),
        },
        {
            "type": "object",
            "description": '文本可伴随图像；文件、视频、音频和记录消息必须独立存在。',
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
            "pattern": "^(?:[\u4e00-\u9fff]{2,6}|[a-z][a-z0-9_-]{0,31})$",
            "description": "用 2–6 个汉字的简短词语描述当前情绪，例如好奇、平静、沮丧或疲惫；不要写英文标签或完整句子。",
        },
        "intensity": {"type": "number", "minimum": 0, "maximum": 1},
        "cause": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["state", "intensity", "cause"],
    "additionalProperties": False,
}

MOOD_CHANGE_TOOL_SPEC: dict[str, Any] = {
    "name": "mood_change",
    "description": "当前支持中文短词作为 state，例如平静、焦急、开心。格式以当前工具 schema 为准，历史报错中的旧限制不代表当前规则。仅持续情绪发生变化时调用，包含自然平复。暂存当前情绪、强度和具体原因，正常结束本轮时提交；没有变化无需调用。",
    "input_schema": MOOD_UPDATE_SCHEMA,
}

MOOD_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        '根据情绪年龄和本轮情况评估持续情绪，忽略短暂反应。状态、强度和原因均准确时仅传 {"decision":"unchanged"}；任一变化（含自然平复）则传 updated 及 state、intensity、cause。必须是对象。'
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

HEARTBEAT_ACTIVITY_TOOL_SPEC: dict[str, Any] = {
    "name": "heartbeat_activity",
    "description": (
        '仅心跳阶段调用，暂存实际活动或休息、结果和下次检查计划。成功提交后，停止调用工具即可结束；结束时原子提交最新报告。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "activity": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
                "description": '本次心跳期间的实际活动或休息情况。',
            },
            "result": {
                "type": "string",
                "maxLength": 2000,
                "description": '具体结果；若无则留空。',
            },
            "next_check_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1440,
                "description": '距离下次自主检查的分钟数；遵守当前心跳上下文中的间隔界限。',
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": '为何此次下次检查符合当前情况。',
            },
        },
        "required": ["activity", "result", "next_check_minutes", "reason"],
        "additionalProperties": False,
    },
}

GOAL_REVIEW_TOOL_SPEC: dict[str, Any] = {
    "name": "goal_review",
    "description": (
        '仅 Goal 阶段调用，暂存当前目标结果、后续行动或调度；结束时提交。目标 ID 自动提供，不传 goal_id/latest_result，只传 status、result 及该状态需要的字段。成功提交后，停止调用工具即可结束本轮。'
    ),
    "input_schema": GOAL_REVIEW_SCHEMA,
}


END_TURN_EXAMPLE = {"mood": {"decision": "unchanged"}}

END_TURN_TOOL_SPEC: dict[str, Any] = {
    "name": "end_turn",
    "description": (
        '结束本轮并提交其暂存状态。不发送消息。可单独调用或在 reply、heartbeat_activity、goal_review 或 save_image_summary 之后最后调用；这些前置调用必须成功。其他工作工具必须在更早的轮次中完成。对于 owner、webhook 和 heartbeat，需提供 mood 对象。Heartbeat 要求在结束前成功调用 heartbeat_activity。对于 Goal，必须先成功调用 goal_review，然后以空参数 {} 调用 end_turn。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mood": MOOD_DECISION_SCHEMA,
        },
        "oneOf": [
            {
                "title": "Conversation completion",
                "required": ["mood"],
                "examples": [copy.deepcopy(END_TURN_EXAMPLE)],
            },
            {
                "title": "Goal completion after goal_review",
                "maxProperties": 0,
                "examples": [{}],
            },
        ],
        "examples": [
            copy.deepcopy(END_TURN_EXAMPLE),
            {},
            {"mood": {"decision": "unchanged"}},
        ],
        "additionalProperties": False,
    },
}


def end_turn_tool_spec(stage: str) -> dict[str, Any]:
    """Return a stage-specific copy for correction details only."""
    spec = copy.deepcopy(END_TURN_TOOL_SPEC)
    schema = spec["input_schema"]
    schema.pop("oneOf")
    if stage == "goal":
        schema["properties"] = {}
        schema["required"] = []
        schema["examples"] = [{}]
    elif stage in {"owner", "heartbeat", "webhook"}:
        schema["required"] = ["mood"]
        schema["examples"] = [copy.deepcopy(END_TURN_EXAMPLE)]
    else:
        raise ValueError(f"end_turn is not available in {stage}")
    return spec


def end_turn_correction(error: str, schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    hints = {
        "end_turn_must_be_alone": "Call end_turn alone or last after reply, heartbeat_activity, goal_review or save_image_summary. Finish other work tools in earlier rounds.",
        "reply_required_before_end_turn": "This recovery phase requires a user-visible notification before ending.",
        "goal_review_required_before_end_turn": "Call goal_review successfully before end_turn({}); they may share a batch in that order.",
        "goal_end_turn_requires_empty_arguments": "Submit the Goal outcome through goal_review; end_turn accepts only {} in this stage.",
        "unexpected_end_turn_fields": "end_turn accepts only mood. Submit Goal outcomes through goal_review and Heartbeat activity and schedule through heartbeat_activity.",
        "heartbeat_activity_required_before_end_turn": "Call heartbeat_activity successfully before end_turn; they may share a batch.",
        "invalid_mood_decision": 'mood must be {"decision":"unchanged"} or {"decision":"updated","state":"平静","intensity":0.3,"cause":"具体原因"}. A string is invalid.',
        "bubbles_not_allowed_in_end_turn": "Send the response through reply first; remove bubbles from end_turn.",
        "activity_not_allowed_in_end_turn": "Remove activity; record Heartbeat activity through heartbeat_activity before ending.",
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
