from typing import Any

from ....memory.metadata import TRIGGER_INPUT_SCHEMA

from ....memory.writing.validation import ALWAYS_MEMORY_KINDS
from ....memory.storage.records import MEMORY_ACTIVATIONS, MEMORY_KINDS
from ....storage.memory.catalog import MOMOI_MEMORY_TAGS

_EVIDENCE = {
    "type": "array",
    "minItems": 1,
    "description": '支持变更及所有已解决请求的确切所有者引用。',
    "items": {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": '提供的已认证所有者事件 ID。',
            },
            "quote": {
                "type": "string",
                "minLength": 1,
                "description": '该事件的精确连续子串。',
            },
        },
        "required": ["event_id", "quote"],
        "additionalProperties": False,
    },
}
_MEMORY = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string", "enum": sorted(MEMORY_KINDS),
            "description": (
                '规范的主题类型：profile、preference、relationship、third_party、practice、world_knowledge、self_insight 或 cross_event_state。具体的/共享的体验是一个 Episode，而非 memory。'
            ),
        },
        "key": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.-]{0,199}$"},
        "content": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "只写事实或规则及必要条件；不附证据、记录日期或审阅经过，保留事实本身必要的日期。"},
        "activation": {"type": "string", "enum": sorted(MEMORY_ACTIVATIONS)},
        "meta": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string", "maxLength": 200,
                    "description": "全局填空字符串；工作流范围为 heartbeat、webhook 或 goal:<提供的 Goal ID>。key 不包含范围前缀。",
                },
                "triggers": TRIGGER_INPUT_SCHEMA,
                "tags": {
                    "type": "array", "maxItems": 3, "uniqueItems": True,
                    "items": {"type": "string", "enum": sorted(MOMOI_MEMORY_TAGS.tags)},
                    "description": "; ".join(f"{key}: {value}" for key, value in MOMOI_MEMORY_TAGS.tags.items()),
                },
            },
            "required": ["tags"], "additionalProperties": False,
            "description": "从预定义目录选择零至三个主题；没有合适主题时 tags 为空。",
        },
        "expires_at": {
            "type": "null",
            "description": '记忆不会过期；始终为 null。临时状态属于当前状态。',
        },
    },
    "required": ["kind", "key", "content", "activation", "expires_at"],
    "oneOf": [
        {
            "properties": {
                "activation": {"enum": ["recall"]},
            }
        },
        {
            "properties": {
                "activation": {"enum": ["always"]},
                "kind": {"enum": sorted(ALWAYS_MEMORY_KINDS)},
            }
        },
        {
            "properties": {
                "activation": {"enum": ["scoped"]},
            }
        },
    ],
    "additionalProperties": False,
}
MEMORY_OPERATION_FINISH_SPEC: dict[str, Any] = {
    "name": "memory_operation_finish",
    "description": '提交完整的决策计划并结束模型审阅，运行时随后复核并原子提交。单独调用。省略的当前记忆保持不变。',
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": 1,
                "description": '恰好解决一次每个提供的操作；合并涉及同一事实的请求。',
                "items": {
                    "type": "object",
                    "properties": {
                        "operation_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "action": {
                            "type": "string",
                            "enum": ["write", "metadata", "forget", "noop", "defer"],
                            "description": (
                                '当持久含义已表示时执行 noop 并提供 target_ids/evidence 追加证据；仅调整标签或触发词用 metadata；写入以细化或整合现有规则，或添加独立事实。不同的措辞或另一个示例本身并不要求写入。'
                            ),
                        },
                        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                        "target_ids": {
                            "type": "array",
                            "description": (
                                '要替换、合并或遗忘的当前记忆 ID。对于现有规则的澄清或有用的新条件，包含该规则的 ID。仅当为尚未表示的独立有用事实时才为空；不同的键或示例并不能使事实独立。'
                            ),
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "memory": _MEMORY,
                        "meta": _MEMORY["properties"]["meta"],
                        "evidence": _EVIDENCE,
                    },
                    "required": ["operation_ids", "action", "reason"],
                    "oneOf": [
                        {
                            "properties": {"action": {"enum": ["write"]}, "meta": False},
                            "required": ["target_ids", "memory", "evidence"],
                        },
                        {
                            "properties": {
                                "action": {"enum": ["forget"]},
                                "meta": False,
                                "target_ids": {"minItems": 1},
                                "memory": False,
                            },
                            "required": ["target_ids", "evidence"],
                        },
                        {
                            "properties": {
                                "action": {"enum": ["noop", "defer"]},
                                "meta": False,
                                "target_ids": False,
                                "memory": False,
                                "evidence": False,
                            }
                        },
                        {
                            "properties": {
                                "action": {"enum": ["noop"]},
                                "target_ids": {"minItems": 1},
                                "memory": False,
                                "meta": False,
                            },
                            "required": ["target_ids", "evidence"],
                        },
                        {
                            "properties": {
                                "action": {"enum": ["metadata"]},
                                "target_ids": {"minItems": 1},
                                "memory": False,
                            },
                            "required": ["target_ids", "meta", "evidence"],
                        },
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["decisions"],
        "additionalProperties": False,
    },
}
MEMORY_OPERATION_SEARCH_SPEC = {
    "name": "memory_operation_search",
    "description": '当提供的记忆无法识别目标或相关重复项时，可选的只读查找。同时做语义与字面候选检索，覆盖各种 activation，并按请求 scope 限制范围。只返回本轮新加入的记录；candidate_ids 包含此前候选。空 memories 不代表没有匹配，不提供分页；需要时换查询。只检索当前有效版本，已删除和被替代的记录不参与。',
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
                "description": (
                    '填写待核对的事实或行为；也可用空格分隔的关键词做字面 OR 匹配。整段输入同时用于向量检索，中文长句不会自动分词；未启用向量时应使用短关键词。不要只搜 key 或拼接无关术语。'
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
