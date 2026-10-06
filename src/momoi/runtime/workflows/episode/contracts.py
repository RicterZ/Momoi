from typing import Any

from ....semantic.cue_contract import CUE_ARCHIVE_CONTRACT


_TURN_IDS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "uniqueItems": True,
    "description": 'T-XX 参考当前请求转录中的 pending_turns。',
    "items": {"type": "string", "pattern": "^T-[1-9][0-9]*$"},
}
_TAGS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
}
_TOPICS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "maxItems": 5,
    "uniqueItems": True,
    "description": "话题最终重点标签，最佳3个、最多5个；按重要性排序，合并同义标签，不凑数。continue时覆盖旧标签，须概括整个话题，不写句子或摘要。",
    "items": {"type": "string", "minLength": 1, "maxLength": 24},
}
_DEFER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["defer"]},
        "turn_ids": _TURN_IDS_SCHEMA,
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["action", "turn_ids", "reason"],
    "additionalProperties": False,
}
_IGNORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ignore"]},
        "turn_ids": _TURN_IDS_SCHEMA,
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["action", "turn_ids", "reason"],
    "additionalProperties": False,
}
_CONTINUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue"]},
        "episode_id": {"type": "string", "minLength": 1},
        "turn_ids": _TURN_IDS_SCHEMA,
        "topics": _TOPICS_SCHEMA,
        "entities": _TAGS_SCHEMA,
        "open_loops": _TAGS_SCHEMA,
    },
    "required": [
        "action",
        "episode_id",
        "turn_ids",
        "topics",
        "entities",
        "open_loops",
    ],
    "additionalProperties": False,
}
_NEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["new"]},
        "key": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$",
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "turn_ids": _TURN_IDS_SCHEMA,
        "topics": _TOPICS_SCHEMA,
        "entities": _TAGS_SCHEMA,
        "open_loops": _TAGS_SCHEMA,
    },
    "required": [
        "action",
        "key",
        "title",
        "turn_ids",
        "topics",
        "entities",
        "open_loops",
    ],
    "additionalProperties": False,
}

EPISODE_CLASSIFY_TURNS_SPEC: dict[str, Any] = {
    "name": "episode_classify_turns",
    "description": (
        '对任何非空且尚未覆盖的 pending Turns 子集保持分类。单次响应中的调用必须使用互不重叠的 Turns 子集。结果报告持久化已覆盖和剩余的 Turns ID。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "oneOf": [
                        _DEFER_SCHEMA,
                        _IGNORE_SCHEMA,
                        _CONTINUE_SCHEMA,
                        _NEW_SCHEMA,
                    ]
                },
            }
        },
        "required": ["decisions"],
        "additionalProperties": False,
    },
}

EPISODE_CONSOLIDATION_FINISH_SPEC: dict[str, Any] = {
    "name": "episode_consolidation_finish",
    "description": (
        '仅在所有 pending Turns 均拥有持久的 Store 决策后才完成分类。运行时会在仍有 Turns 剩余时拒绝此调用。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

_SUMMARY_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": '从 previous_verified_claims 或 new_messages 复制引用元数据。',
    "properties": {
        "message_id": {"type": "integer", "minimum": 1},
        "turn_id": {"type": "string", "minLength": 1},
        "ordinal": {"type": "integer", "minimum": 1},
        "quote": {
            "type": "string", "minLength": 1, "maxLength": 1000,
            "description": '被引用的原始消息的精确连续子串。',
        },
    },
    "required": ["message_id", "turn_id", "ordinal", "quote"],
    "additionalProperties": False,
}

EPISODE_SUMMARY_FINISH_SPEC: dict[str, Any] = {
    "name": "episode_summary_finish",
    "description": (
        '提交所声称 Episode 的完整证据支持的作业摘要。运行时会针对归档的原始消息验证每个引用后再提交。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "uniqueItems": True,
                "items": _SUMMARY_CLAIM_SCHEMA,
            },
            "narrative_summary": {
                "type": "string", "maxLength": 1200,
                "description": (
                    '证据支持的 5W1H 摘要：what、who、when、where、why、how。在简洁的行文中涵盖所有六个要素，不重复事实或需要单独标签。明确标记未陈述或不适用要素；不要推断缺失事实。保留有用的名称、数字、时间顺序、更正和未解决结果。区分所有者陈述、助手推断、承诺和已验证结果。仅在必要时引用更正后的措辞；不要复述重复的措辞。保持摘要在 1000 个字符以内。'
                ),
            },
            "emotional_context": {
                "type": "object",
                "description": '证据支持的感受和语气；未知处使用空字符串。',
                "properties": {
                    "owner": {"type": "string", "maxLength": 300},
                    "assistant": {"type": "string", "maxLength": 300},
                    "tone": {"type": "string", "maxLength": 300},
                },
                "required": ["owner", "assistant", "tone"],
                "additionalProperties": False,
            },
            "recall_cues": {
                "type": "array", "maxItems": 5, "uniqueItems": True,
                "description": CUE_ARCHIVE_CONTRACT + " Empty if none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 100},
                        "evidence_message_ids": {
                            "type": "array", "minItems": 1,
                            "items": {"type": "integer"}, "uniqueItems": True,
                        },
                    },
                    "required": ["text", "evidence_message_ids"],
                    "additionalProperties": False,
                },
            },
            "outcomes": {
                "type": "array",
                "maxItems": 12,
                "description": '简洁的已完成结果、决策或变更，而非任务列表。',
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        },
        "required": [
            "claims",
            "narrative_summary",
            "emotional_context",
            "outcomes",
            "recall_cues",
        ],
        "additionalProperties": False,
    },
}
