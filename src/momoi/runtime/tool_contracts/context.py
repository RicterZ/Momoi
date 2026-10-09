import copy
from typing import Any

CUE_QUERY_TOOL_DESCRIPTION = (
    '用对话语言描述当前需要的历史场景、意图、人物、任务和关键词，保留可区分的姓名与标识符。仅依据已知上下文解析指代、缩小范围，不猜答案或关联无关事件。semantic 直接描述要找的内容，不加“此前”“之前讨论过”等检索动作或时间前缀；只有时间本身是检索条件时才写具体时间。无需知道原始措辞；同时语义检索话题摘要及单独嵌入的 CUES。'
)
from ...tools.contracts.memory import MEMORY_TOOL_SPECS
from ...memory.storage.records import MEMORY_KINDS

_MEMORY_FILTERS = copy.deepcopy(next(s for s in MEMORY_TOOL_SPECS if s["name"] == "memory_search")["input_schema"]["properties"]["filters"])
_MEMORY_FILTERS["properties"].pop("kinds")


RECALL_SCOPE_CONTRACT = (
    'semantic 和 keyword 可省略或为空；仅指定 kind、tags 或 scope 时按条件获取记忆，不检索 Episode。'
    '至少提供一个查询或筛选条件。semantic 可提供互补查询，无需凑满数量，不把未知答案写成事实。'
    'keyword 是所有查询共用的字面 OR 锚点。tags 和 scope 只过滤已确认记忆，不限制 Episode 话题；'
    '普通搜索省略 tags，避免漏掉未标标签的相关记忆。需要原文时用 episode_read。'
)


def recall_correction(message: str) -> dict[str, Any]:
    return {
        "message": message + " 请重新以原生 recall 工具调用；本次检索尚未成功。",
        "hint": "semantic、keyword、tags、kind、scope 均可省略，至少提供一个有效查询或筛选条件。"
                "不传 units、intent、mode 或来源轮次，不把数组编码为字符串。",
    }


RECALL_TOOL_SPEC: dict[str, Any] = {
    "name": "recall",
    "description": (
        "检索已确认记忆和话题摘要。需要缺失的历史证据时调用，当前上下文充分时无需调用。"
        "依赖结果的操作须等待检索成功；同一回合可追加搜索，每次返回本次证据，已有证据保留在上下文。"
        + RECALL_SCOPE_CONTRACT
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "semantic": {
                "type": "array", "minItems": 0, "maxItems": 6, "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"\S"},
                "description": CUE_QUERY_TOOL_DESCRIPTION,
            },
            "keyword": {
                "type": "array", "maxItems": 6, "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 60, "pattern": r"\S"},
                "description": "可选，共用的字面关键词 OR 锚点；只用已知名称或具体短语，无可靠词时省略。",
            },
            "kind": {
                "type": "array", "uniqueItems": True, "maxItems": len(MEMORY_KINDS),
                "items": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                "description": "可选记忆性质白名单（OR）：profile 档案习惯，preference 偏好，relationship 关系约定，third_party 他人，practice 方法，world_knowledge 知识，self_insight 自我洞见，cross_event_state 跨事件状态。默认不限制；不影响 Episode。",
            },
            "tags": copy.deepcopy(_MEMORY_FILTERS["properties"]["tags_any"]),
            "scope": copy.deepcopy(_MEMORY_FILTERS["properties"]["scope"]),
        },
        "additionalProperties": False,
        "anyOf": [
            {"required": [name], "properties": {name: {"minItems": 1}}}
            for name in ("semantic", "keyword", "kind", "tags")
        ] + [{"required": ["scope"], "properties": {"scope": {"minLength": 1}}}],
    },
}


def recall_search_arguments(arguments):
    """One validation boundary for owner, heartbeat and relation lookup."""
    from ...tools.validation import validate_tool_arguments
    parsed, error = validate_tool_arguments("recall", arguments, RECALL_TOOL_SPEC["input_schema"])
    if error:
        raise ValueError("recall requires a query or filter; invalid fields or values")
    return {
        "semantic": list(dict.fromkeys(" ".join(value.split()) for value in parsed.get("semantic", []))),
        "keyword": list(dict.fromkeys(" ".join(value.split()) for value in parsed.get("keyword", []))),
        "filters": {"tags_any": parsed.get("tags", []), "kinds": parsed.get("kind", []), "scope": parsed.get("scope", "")},
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
