import copy

from .conversation import CHANNEL_BUBBLE_SCHEMA


MEDIA_BUBBLE_SCHEMA = copy.deepcopy(CHANNEL_BUBBLE_SCHEMA["oneOf"][1:])
# A text-only segment would bypass expression generation; captions may accompany media.
MEDIA_BUBBLE_SCHEMA[0]["properties"]["segments"]["contains"] = {
    "properties": {"type": {"not": {"enum": ["text", "reply", "at"]}}},
    "required": ["type"],
}

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
            "mode": {"type": "string", "enum": ["text", "voice"], "default": "text",
                     "description": "发送形式：text 为文字气泡，voice 为语音；可用能力见工具描述。"},
            "attachments": {
                "type": "array", "minItems": 1,
                "description": "随文字回应发送的媒体、附件或表情，原样交给发送层；不放普通发言文本，仅用于 mode=text。",
                "items": {"oneOf": [
                    {"type": "string", "pattern": "^emotion://[a-zA-Z0-9_-]+$",
                     "description": "表情目录中真实存在的 emotion:// 标识。"},
                    *MEDIA_BUBBLE_SCHEMA,
                ]},
            },
        },
        "required": ["intent", "reference"],
        "additionalProperties": False,
        "allOf": [{"if": {"properties": {"mode": {"const": "voice"}}, "required": ["mode"]},
                   "then": {"not": {"required": ["attachments"]}}}],
    },
}
