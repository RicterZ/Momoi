MAINTENANCE_ACTIONS = {"replace", "merge", "retire"}
MEMORY_MAINTENANCE_RUN_VERSION = "v1"

_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                '从<owner_evidence>逐字复制 event_id，包括其频道前缀。'
            ),
        },
        "quote": {
            "type": "string",
            "minLength": 1,
            "description": (
                '该事件内容的精确连续子串；不要改写。'
            ),
        },
    },
    "required": ["event_id", "quote"],
    "additionalProperties": False,
}

MEMORY_MAINTENANCE_FINISH_SPEC: dict[str, object] = {
    "name": "memory_maintenance_finish",
    "description": (
        '应用确认的记忆维护批次并结束此私有 Turns。'
    ),
    "input_schema": {
        "type": "object",
        "description": '精确覆盖每个可变 ID 一次：在 reviewed_ids 中保持不变，已变更，或在 regroup anchor_ids 中推迟。',
        "properties": {
            "reviewed_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "integer", "minimum": 1},
                "description": (
                    '保持不变的内存 ID。'
                ),
            },
            "changes": {
                "type": "array",
                "description": (
                    '可变记忆的变更状态。'
                ),
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "description": (
                                '替换一行可变行。'
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["replace"],
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        '来自<mutable_memories>的目标 ID。'
                                    ),
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                    "description": (
                                        '新版本的完整最终结论及必要条件；不附证据、记录日期或审阅经过，保留事实本身必要的日期。不要扩大支持的事实。'
                                    ),
                                },
                                "triggers": {
                                    "type": "array", "maxItems": 8, "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1, "maxLength": 40},
                                    "description": "最终字面触发词；省略继承，[] 清空。降为 recall 时可按原事实补充具体词，不能猜测新含义。",
                                },
                                "activation": {
                                    "type": "string",
                                    "enum": ["always", "recall", "scoped"],
                                    "description": (
                                        '最终激活。'
                                    ),
                                },
                                "expires_at": {
                                    "type": "null",
                                    "description": (
                                        '始终为 null；记忆不会过期。临时状态属于当前状态。'
                                    ),
                                },
                                "evidence": {
                                    "oneOf": [
                                        _EVIDENCE_SCHEMA,
                                        {"type": "null"},
                                    ],
                                    "description": (
                                        '任何事实更正所需的精确所有者证据。仅在对象、范围、条件、持续时间和极性未变更时使用 null。'
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        '简短的审计理由，解释变更内容及原因。'
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "memory_id",
                                "content",
                                "activation",
                                "expires_at",
                                "evidence",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "description": (
                                '选择一条可变记录的 kind/key，创建新版本并吸收所有源行。'
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["merge"],
                                },
                                "survivor_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        '提供 kind/key 的可变 ID；排除在 source_ids 之外，也会被新版本取代。'
                                    ),
                                },
                                "source_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {"type": "integer", "minimum": 1},
                                    "description": (
                                        '其他可变 ID。所有旧 ID 通过 superseded_by 指向新版本。'
                                    ),
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                    "description": (
                                        '新版本的完整最终结论及必要条件；不附证据、记录日期或审阅经过，保留事实本身必要的日期。'
                                    ),
                                },
                                "triggers": {
                                    "type": "array", "maxItems": 8, "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1, "maxLength": 40},
                                    "description": "最终字面触发词；省略继承，[] 清空。降为 recall 时可按原事实补充具体词，不能猜测新含义。",
                                },
                                "activation": {
                                    "type": "string",
                                    "enum": ["always", "recall"],
                                    "description": (
                                        '最终激活；仅当每行合并行始终合法时才总是合法。'
                                    ),
                                },
                                "expires_at": {
                                    "type": "null",
                                    "description": (
                                        '始终为 null；记忆不会过期。'
                                    ),
                                },
                                "evidence_event_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1},
                                    "description": (
                                        '支持最终内容的来自<owner_evidence>的精确 event_id 字符串；逐字复制。'
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        '简短的审计理由，证明这些行为何属于同一事实/事件，而非仅仅相关。'
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "survivor_id",
                                "source_ids",
                                "content",
                                "activation",
                                "expires_at",
                                "evidence_event_ids",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "description": (
                                '退役一个可变事实。过期的最近行由运行时清除。'
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["retire"],
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        '用于退役的可变标识符。'
                                    ),
                                },
                                "evidence": _EVIDENCE_SCHEMA,
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        '简短的审计理由，指明显式的所有者撤销。'
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "memory_id",
                                "evidence",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "regroup_requests": {
                "type": "array",
                "description": (
                    '推迟需要相关只读 ID 提升为后续可变组的锚点。'
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "anchor_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": (
                                '必须等待的可变 ID。'
                            ),
                        },
                        "include_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": (
                                '仅在上下文/目录中发现的相关 ID；它们不能已经是可变的。'
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 400,
                            "description": '这些 ID 为何需要一个后续审查组。',
                        },
                    },
                    "required": ["anchor_ids", "include_ids", "reason"],
                    "additionalProperties": False,
                },
            },
            "summary": {
                "type": "string",
                "maxLength": 500,
                "description": (
                    '保留、变更和推迟的私有审计摘要。'
                ),
            },
        },
        "required": [
            "reviewed_ids",
            "changes",
            "regroup_requests",
            "summary",
        ],
        "additionalProperties": False,
    },
}
