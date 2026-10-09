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
                    '按话题分段的中文日记，每段以【话题标题】开头，记录本时段有意义的经历、感受、理解变化及必要的未决事项。同一事件跨话题不重复，不要求每个话题都产出内容。'
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
                    '关于主人的当天具体行为、选择、事实、偏好、生活状态及互动约定，可为空。不收集助手自省、工具经验或任务进度。允许单次样本，但须限定为当天所见，不直接推断长期规律；完整事件经过留在 Episode。观察按天保存，暂不参与日常召回。'
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
                                '简述当天观察、情境及不确定性，区分直接事实与推测。单次样本不写成通常规律，反复提及不算独立样本。标明新发生、回顾旧事或状态延续，实际事件时间已知时写明；不把助手建议当作主人已采纳。'
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
