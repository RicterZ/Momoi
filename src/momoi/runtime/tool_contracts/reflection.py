from typing import Any

from ...memory.storage.records import MEMORY_KINDS


REFLECTION_FINISH_SPEC: dict[str, Any] = {
    "name": "reflection_finish",
    "description": (
        '存储当日日记、待跨天验证的观察和对话收尾决定，然后结束此私有 Turn。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 6000,
                "description": (
                    '有意义经历、感受、观点、理解变化及未决问题的中文日记。'
                ),
            },
            "conversation_actions": {
                "type": "array",
                "maxItems": 32,
                "description": (
                    '<open_conversations>的维护工作；无需时为空。'
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "episode_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": '来自<open_conversations>的话题 ID。',
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
                "description": (
                    '关于主人的一般事实、明确的通用偏好、持续状态，或值得跨天比较的行为样本，可为空。不是话题摘要；偶发事故、物品处置、任务过程和当场评价通常不收集。单次样本不等于习惯。观察按天保存，暂不参与日常召回。'
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(MEMORY_KINDS),
                            "description": (
                                '本轮围绕主人分类：profile 为主人事实与具体行为；preference 为主人明确偏好及需求；relationship 为主人关系、称呼与互动约定；third_party 为与主人有关的他人信息；cross_event_state 为主人持续状态。其他类型为存储兼容保留，本轮不收集助手实践、自省或一般知识。类型仅用于分类，不要求逐类填写，也不代表观察已成为稳定规律。'
                            ),
                        },
                        "key": {
                            "type": "string",
                            "description": '稳定的小写点分隔主题键，不含日期；运行时自动添加日期 namespace，避免跨天覆盖。',
                        },
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1000,
                            "description": (
                                '简短一句话，只写一个关于主人的信息点。不带日历日期、引文、事件经过或助手建议。行为样本用“这次”等限定，不提前归纳习惯；持续状态保留“正在”等限定，必要的情境与不确定性不省略。日期及原始依据由复盘记录和 evidence 承担。'
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": (
                                '支持该结论的来自所提供当日或工具证据的精确连续引文，而非仅其主题。'
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": (
                                '证据对本条观察及其范围的支持程度，不代表习惯已稳定、出现频率或重要性。'
                            ),
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
