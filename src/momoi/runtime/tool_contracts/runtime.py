from typing import Any


def tool_search_spec(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    index = "\n".join(
        f"- {spec['name']}: {str(spec.get('description') or '').strip()}"
        for spec in sorted(catalog, key=lambda item: item['name'])
    )
    return {
        "name": "tool_search",
        "description": (
            "按工具名、前缀或描述关键词搜索 MCP 工具，只将命中的工具加载到本轮后续请求；"
            "不执行业务，不解除阶段权限限制。新 turn 不保留发现状态。\n工具索引：\n" + index
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "工具名、前缀或描述关键词；精确工具名优先匹配。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "最多加载的工具数，默认 5。"},
            },
            "required": ["query"],
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
