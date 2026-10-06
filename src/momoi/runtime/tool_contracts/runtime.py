from typing import Any


def tool_search_spec() -> dict[str, Any]:
    return {
        "name": "tool_search",
        "description": "按 system 工具索引中的服务名、服务描述或具体工具名与描述关键词查找工具，返回候选名称和描述。选定后调用 tool_enable 加载完整参数，再使用工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "服务名、服务描述关键词、工具名或工具描述关键词。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "最多返回的候选数，默认 5。"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


TOOL_ENABLE_SPEC = {
    "name": "tool_enable",
    "description": "按精确工具名批量加载 MCP 工具的完整参数。可使用 tool_search 返回的名称或历史记录中已知的名称；重复加载去重。启用状态保留至共享 transcript 压缩，不解除当前阶段权限限制。",
    "input_schema": {
        "type": "object",
        "properties": {
            "tools": {"type": "array", "minItems": 1, "maxItems": 20,
                      "items": {"type": "string", "minLength": 1},
                      "description": "要加载的精确工具名列表。"},
        },
        "required": ["tools"],
        "additionalProperties": False,
    },
}


READ_TOOL_RESULT_SPEC: dict[str, Any] = {
    "name": "read_tool_result",
    "description": (
        '继续截断的工具结果快照，无需重新运行工具。无法读取工作区文件。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "string",
                "pattern": "^tr_[0-9a-f]{32}$",
                "description": '从截断的结果中复制未更改的 result_ref。',
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": '前一个分片中的最新 next_cursor；首个分片可省略。',
            },
        },
        "required": ["result_ref"],
        "additionalProperties": False,
    },
}
