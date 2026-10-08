"""Read-only graph traversal tool for archived topic relationships."""

EPISODE_RELATIONS_TOOL_SPEC = {
    "name": "episode_relations",
    "description": "按话题 ID 分页查看已有的关联，包含该话题指向别人的关系和别人指向该话题的关系。可展开一层或两层；默认每页 20 条关系，使用 next_cursor 续读，节点只带短摘要；仅返回有效的已建关系，不包含无关审阅记录。",
    "input_schema": {
        "type": "object",
        "properties": {
            "cursor": {"type": "integer", "minimum": 0, "description": "上一页 next_cursor，首页省略。"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "episode_id": {"type": "string", "minLength": 1, "description": "要查看的话题 ID。"},
            "depth": {"type": "integer", "enum": [1, 2], "default": 1,
                      "description": "沿关系展开的层数，默认 1。"},
        },
        "required": ["episode_id"],
        "additionalProperties": False,
    },
}
