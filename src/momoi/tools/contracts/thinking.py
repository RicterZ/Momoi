from typing import Any

THINKING_TOOL_POLICY = """### 思考记录工具

用户询问助手为什么做了或没做某事，或最近某个 Turn 如何作出决定时，使用 `thinking_search` 和 `thinking_read`。这些记录是过去模型调用留下的、可能有误的线索，不是当前规则，也不是已发送给用户的消息。以发件箱和对话中的事实为准。不要向用户倾倒原始思考记录；说明结论和必要证据。
"""

THINKING_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "thinking_search",
        "description": (
            '按回合、关键词或时间搜索已记录的模型思考过程，返回精简摘录而非完整推理。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "turn_id": {
                    "type": "string",
                    "description": '精确的回合 ID。',
                },
                "query": {
                    "type": "string",
                    "description": (
                        '字面关键词用空格分隔，词之间为 OR，例如：部署 Docker。'
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        '窗口期；若无 turn_id，默认为 30 天。'
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["recent", "range", "all"],
                        },
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3650,
                        },
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                "stage": {
                    "type": "string",
                    "description": (
                        '调用阶段，例如 owner、webhook、heartbeat、goal、reflection 等。'
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "cursor": {
                    "type": "integer",
                    "minimum": 0,
                    "description": '偏移量作为 next_cursor 返回。',
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "thinking_read",
        "description": (
            '从 thinking_search 读取某回合的思考记录。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "turn_id": {
                    "type": "string",
                    "minLength": 1,
                },
                "call_id": {
                    "type": "string",
                    "description": '来自 thinking_search 的调用 ID；省略则读取该回合的所有调用。',
                },
            },
            "required": ["turn_id"],
            "additionalProperties": False,
        },
    },
]
