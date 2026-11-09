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
                '每个字符串作为一个独立气泡发送。emotion:// 的值必须精确匹配 <emotion_catalog> 中列出的 emotion://<listed-slug>；它将发送一个独立的反应图像。'
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
