from typing import Any

from ...storage import MEMORY_KINDS


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
                    '值得跨天比较的当天观察，可为空。允许单次样本，但须限定为当天所见，不直接推断长期规律；完整事件经过留在 Episode。观察按天保存，暂不参与日常召回。'
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(MEMORY_KINDS),
                            "description": (
                                'profile：用户身份、背景、节奏与习惯；preference：用户的需求，包括约束条件及既定措辞；relationship：关系纽带及其边界、称呼方式与约定；third_party：关于他人的观察；practice：方法或决策过程及其结果（含工具使用）；world_knowledge：对外部事物的认识；self_insight：对自身感受或倾向的主观理解；cross_event_state：超越产生它的事件而持续存在的状态。类型仅用于分类，不要求逐类填写，也不代表观察已成为稳定规律。'
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
                                '简述当天观察、情境及不确定性，区分直接事实与推测。单次样本不写成通常规律，反复提及不算独立样本。practice 包含适用条件和可观察结果；未受批评不是成功证据。'
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
