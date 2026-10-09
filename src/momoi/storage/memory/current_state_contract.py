"""Single source for state mutation fields, limits and model-facing guidance.

The private maintenance workflow uses this schema and passes its arguments to
CurrentStateManager.apply_arguments. Foreground end_turn does not carry state.
"""

MAX_SLOTS = 12
SLOT_SOFT_WARNING_THRESHOLD = 8
MAX_TTL_SECONDS = 24 * 3600
SUBJECT_MAX_LENGTH = 128
KEY_MAX_LENGTH = 64
KEY_PATTERN = r"^[a-z][a-z0-9_.-]*(?![\s\S])"
VALUE_MAX_LENGTH = 512
ID_MAX_LENGTH = 128
CURRENT_STATE_SOURCE_STAGES = frozenset({"owner", "goal", "heartbeat", "webhook", "reply_followup", "plan_step"})
# Current-state maintenance is temporarily driven only by completed owner Turns.
# Keep CURRENT_STATE_SOURCE_STAGES broad because it also controls which stages
# may receive current-state context; this narrower set controls task staging.
CURRENT_STATE_TRIGGER_STAGES = frozenset({"owner"})

CURRENT_STATE_CHANGE_SCHEMA = {
    "type": "object",
    "description": (
        '若无变更，返回空的添加和删除数组。长期偏好属于记忆；计划性工作属于目标。所有变更均经过验证并原子性提交。'
    ),
    "properties": {
        "delete": {
            "type": "array",
            "maxItems": MAX_SLOTS,
            "uniqueItems": True,
            "description": (
                '现有槽位的 ID 与新证据相矛盾或已被新证据终结。替换需在同一变更集中原子性地删除旧槽位并添加其继任者。请使用当前维护快照中的 ID。后端处理基于时间的过期。'
            ),
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": ID_MAX_LENGTH,
                "pattern": r"\S",
            },
        },
        "add": {
            "type": "array",
            "maxItems": MAX_SLOTS,
            "description": '由新证据支持的状态；在适用时复用现有维度。',
            "items": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": SUBJECT_MAX_LENGTH,
                        "pattern": r"\S",
                        "description": '已知状态持有者。区分用户、助手及其他已确立的实体。',
                    },
                    "key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": KEY_MAX_LENGTH,
                        "pattern": KEY_PATTERN,
                        "description": (
                            '状态维度。应复用现有键而非发明同义词。每个主题和键仅允许一个活动槽位。'
                        ),
                    },
                    "value": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": VALUE_MAX_LENGTH,
                        "pattern": r"\S",
                        "description": '由证据支持的简洁当前事实或已决定的近期安排，保留归因与不确定性。不惯例性添加日期、星期前缀；仅保留影响安排、计算或区分事实的必要时间。',
                    },
                    "status": {
                        "type": "string", "enum": ["observed", "inferred"],
                        "description": '观察到的内容直接陈述自来源，而非独立证明。标记推论得出的内容。',
                    },
                    "source_turn": {
                        "type": "string", "minLength": 1,
                        "description": '本对话转录中该证据 Turn 的精确 T-N 标签。',
                    },
                    "source_id": {
                        "type": "string", "minLength": 1,
                        "description": '来自 source_evidence（event:... 或 message:...）的精确源 ID。',
                    },
                    "source": {
                        "type": "string", "minLength": 1, "maxLength": 512,
                        "description": '来自单一源消息的精确连续引用。切勿将助手的言论归因于用户。运行时解析说话者与证据时间。',
                    },
                    "uncertainty": {
                        "type": "string", "maxLength": 512,
                        "description": '尚未确认的内容；对于推论状态必须非空，否则可为空。',
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TTL_SECONDS,
                        "description": (
                            '新证据支持保留该事实的时长，从提交时间开始计算。仅保留最多 24 小时的由证据支持的窗口；未来行动不排除其当前有效的约束。仅凭更长持续时间并不使事实持久。对于真正持久的事实，请使用 memory_operation 而非当前状态槽位。看到或复用槽位并非续期的证据。过期意味着未知，而非相反状态成立。续期需要新证据并执行删除 + 添加。'
                        ),
                    },
                },
                "required": ["subject", "key", "value", "ttl_seconds", "status", "source_turn", "source_id", "source", "uncertainty"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["add", "delete"],
    "additionalProperties": False,
}
