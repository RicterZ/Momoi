import copy
import json
from typing import Any

CUE_QUERY_TOOL_DESCRIPTION = (
    '使用对话的语言来表达检索场景、意图及相关内容、人物、任务或关键词。保留有助于区分主题的姓名和标识符。不要捏造事实或将不相关的事件联系起来。基于当前对话，撰写一个自然语言查询，描述当前所需的历史信息。归档提示描述了未来可能需要记忆的场景；当前查询则表达该需求本身。两者通过相同的场景、意图及相关内容进行语义匹配。仅使用已知上下文来缩小查询范围并解析引用。不要猜测未知答案；知道原始提示的措辞并非必需。该查询同时搜索话题摘要和单独嵌入的 CUES。'
)
from ...storage import MEMORY_KINDS


RECALL_SKIP_EXAMPLE = {"units": [{
    "intent": "主人道晚安", "recall_mode": "skip", "recall_queries": [],
    "recall_from_turn_id": "",
}]}
RECALL_EXAMPLES = [
    RECALL_SKIP_EXAMPLE,
    {"units": [{"intent": "查询此前约定的见面时间", "recall_mode": "search",
                "recall_queries": [{"semantic": "此前约定的见面时间", "keywords": []}],
                "recall_from_turn_id": ""}]},
    {"units": [{"intent": "继续讨论已检索的约定", "recall_mode": "reuse",
                "recall_queries": [], "recall_from_turn_id": "<displayed-recalled-turn-id>",
                }]},
]

RECALL_SCOPE_CONTRACT = (
    '检索范围：评估所供上下文是否遗留可能改变理解或行动的历史问题。若无此类问题则使用 skip。对于搜索，使用范围最小的 kind 列表（空/省略表示所有标准记忆类型）。根据历史信息可能采用的不同表述，从当前提法、相关称谓、事件或其他已知线索构造互补查询；即使查询指向同一需求，也可以从不同角度搜索。无需凑满查询数；避免只换词重复、依赖单个新细节或把未知答案写成事实。关键词仅用可靠、可区分的锚点，不用独立动词或通用词。仅在已显示的 recent_recall_context 查询集覆盖全部需求时使用 reuse；邻近性与情绪不构成覆盖。将独立结果拆分为不同单元；更正操作替换被撤销的意图。保留已知主体、字面标识符及不确定性；切勿臆造未解决的实体身份。若身份未解决，先进行搜索，若证据无法识别则询问。从上下文中解析代词，切勿臆造答案或新增检索需求；当上下文无法解析指代时寻求澄清。reuse 无需新的历史依赖。在评估证据后决定如何响应。已检索话题的置信度是受限的查询相关性信号，而非校准后的概率或事实真相。缺失值表示无可用的查询特定分数。从源证据中确立发生的事件，并考虑说话者、时间、模态及不确定性。'
)


def recall_correction(message: str) -> dict[str, Any]:
    return {
        "message": message + " 请重新以原生 recall 工具调用；本次检索尚未成功。",
        "hint": (
            "intent 等字段必须放在 units 内（1 到 4 个对象），不能放在顶层。"
            "使用 JSON 数组和对象，不能把 JSON 编码成字符串。search 需要 1 到 3 个查询，"
            "且 recall_from_turn_id 为空；reuse 需要空查询数组，并填写已显示的 recalled Turn ID；"
            "skip 需要空查询数组和空 ID。只根据实际证据填写；示例仅表示上下文充分时可以 skip。"
            "每个单元可以将 kind 设为空数组（所有规范记忆类型），或填写例如"
            "[\"profile\", \"preference\"] 的类型列表。话题摘要单独检索。"
        ),
        "example_arguments": copy.deepcopy(RECALL_SKIP_EXAMPLE),
    }


RECALL_TOOL_SPEC: dict[str, Any] = {
    "name": "recall",
    "description": (
        ('检索已确认的记忆、带日期的反思和话题摘要。在用户回合中，在首批工具调用中调用一次；独立工具可伴随其运行。在心跳周期中，于发现特定内容后且每次向用户可见发送前进行搜索；使用一个搜索单元，并等待结果后再发送。心跳周期的检索不会归档话题。重试直至成功；在依赖调用前等待其结果。同一回合中的后续调用可检索额外证据。每次调用均增加证据。后续单元可描述新的搜索角度；跳过操作不清除早期结果。新的用户消息可修订意图。skip/reuse 返回状态时不会重复返回 transcript 中已有的证据；搜索返回本次调用的证据而非累积的早期结果。每次调用及结果均保留在回合转录中。参数必须包含 units（意图对象数组）；切勿扁平化其字段或将嵌套 JSON 字符串化。' + RECALL_SCOPE_CONTRACT + '最小示例（上下文充足时）：' + json.dumps(RECALL_SKIP_EXAMPLE, ensure_ascii=False))
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "units": {
                "type": "array",
                "description": '必填数组：每个独立的用户意图对应一个对象，而非 JSON 字符串。',
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": r"\S",
                            "maxLength": 160,
                            "description": (
                                '客观描述用户的当前请求或共享信息，并纳入修正内容。保留不确定性；不要添加未陈述的需求或您计划的响应策略。'
                            ),
                        },
                        "kind": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": len(MEMORY_KINDS),
                            "uniqueItems": True,
                            "items": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                            "description": (
                                '可选的记忆种类白名单。为空或省略表示所有规范种类；使用此字段以避免导入不相关的记忆。话题摘要单独搜索，不属于种类。'
                            ),
                        },
                        "recall_mode": {
                            "type": "string",
                            "enum": ["search", "reuse", "skip"],
                            "description": (
                                '搜索缺失的历史证据；复用之前的查询范围；当提供的上下文已足够时跳过。'
                            ),
                        },
                        "recall_queries": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": 3,
                            "description": (
                                '同一需求可从不同已知线索或表述角度搜索；查询应互补，避免仅换词重复。'
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "semantic": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 240,
                                        "description": CUE_QUERY_TOOL_DESCRIPTION,
                                    },
                                    "keywords": {
                                        "type": "array",
                                        "minItems": 0,
                                        "maxItems": 6,
                                        "items": {"type": "string", "maxLength": 60},
                                        "description": (
                                            '关键词 OR 锚点：字面准确名称、ID、标题或独特的支持事件短语。不得包含独立动词、代词、通用词汇或推断出的答案；若无可靠锚点则为空。'
                                        ),
                                    },
                                },
                                "required": ["semantic"],
                                "additionalProperties": False,
                            },
                        },
                        "recall_from_turn_id": {
                            "type": "string",
                            "description": ('recent_recall_context 中的来源轮次。'),
                        },
                    },
                    "required": [
                        "intent",
                        "recall_mode",
                        "recall_queries",
                        "recall_from_turn_id",
                    ],
                    "oneOf": [
                        {
                            "properties": {
                                "recall_mode": {"enum": ["search"]},
                                "recall_queries": {"minItems": 1},
                                "recall_from_turn_id": {"const": ""},
                            }
                        },
                        {
                            "properties": {
                                "recall_mode": {"enum": ["reuse"]},
                                "recall_queries": {"maxItems": 0},
                                "recall_from_turn_id": {"minLength": 1},
                            }
                        },
                        {
                            "properties": {
                                "recall_mode": {"enum": ["skip"]},
                                "recall_queries": {"maxItems": 0},
                                "recall_from_turn_id": {"const": ""},
                            }
                        },
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "examples": copy.deepcopy(RECALL_EXAMPLES),
        "required": ["units"],
        "additionalProperties": False,
    },
}


def heartbeat_begin_spec() -> dict[str, Any]:
    return {
        "name": "heartbeat_begin",
        "description": (
            '开始所选自主活动；需要外部工具时用 tool_search 查找候选，再用 tool_enable 加载选定工具。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "activity": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": (
                        '本次 Heartbeat 中您将执行的操作或体验。'
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["work", "rest"],
                },
                "strategy": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300,
                    },
                    "description": (
                        '最小有序检查、结果分支及完成或延续条件。'
                    ),
                },
            },
            "required": [
                "activity",
                "mode",
                "strategy",
            ],
            "allOf": [
                {
                    "oneOf": [
                        {
                            "properties": {
                                "mode": {"enum": ["work"]},
                                "strategy": {"minItems": 1},
                            }
                        },
                        {
                            "properties": {
                                "mode": {"enum": ["rest"]},
                                "strategy": {"maxItems": 0},
                            }
                        },
                    ]
                },
            ],
            "additionalProperties": False,
        },
    }
