REPLY_TOOL_SPEC = {
    "name": "reply",
    "description": "把本次回应意图和必要依据交给 Replyer 生成并发送实际发言；返回实际内容和投递结果。",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "minLength": 1, "maxLength": 1000,
                       "description": "回应方向、情绪、态度或要问的问题，不是已经写好的完整回复。"},
            "reference": {"type": "string", "maxLength": 6000,
                          "description": "必要事实、查询结论、时间数字及承诺边界；与当前回应无关的材料不传。"},
            "mode": {"type": "string", "enum": ["text", "voice"], "default": "text"},
        },
        "required": ["intent", "reference"],
        "additionalProperties": False,
    },
}
