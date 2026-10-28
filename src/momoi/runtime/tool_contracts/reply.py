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
    "description": "把本次回应意图和必要依据交给 Replyer 生成并发送实际发言；等待发送完成或被打断后返回。bubbles 只含确认发出的内容，其 [image: …]、[file: …] 等是媒体摘要，不代表发送了路径文字。deliveries 逐条返回 outbox_id、kind、segment_types、state 及频道返回的 message_id（如有）；sent 表示频道确认发送，不能据此判断对方已查看或图片具体内容；failed/superseded 为失败/取消，uncertain 为投递不确定，pending/ambiguous 仍可能排队重试。发送超时仍可能在队列中，不要重复发送。",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "minLength": 1, "maxLength": 1000,
                       "description": "本次回应的目标或要问的问题；涉及双方处境、要求或承诺时明确写用户或助手，不省略主语；与 reference 的来源事实核对一致后提交。不代写台词片段，不指定措辞、句数或角色表演方式，不擅自追加邀约、承诺或无必要的追问。"},
            "reference": {"type": "string", "maxLength": 6000,
                          "description": "必要事实、查询结论、时间数字、承诺边界及用户明确的表达约束；涉及旧话时标明说话人并引用原文，区分用户要求与助手安排，不把助手发言当成用户确认。不放台词草稿或无关材料。"},
            "mode": {"type": "string", "enum": ["text", "voice"], "default": "text",
                     "description": "发送形式：text 为文字气泡，voice 为语音；可用能力见工具描述。"},
            "reply_to_message_id": {"type": "string", "minLength": 1,
                "description": "可选，仅 QQ 文字回应：需要突出回应某条消息时，填写当前输入或 recall、episode_read 原文 quote_targets 中的 QQ message_id（不是数据库记录 id）；普通回应省略。支持历史原文，只引用第一个文字气泡。"},
            "attachments": {
                "type": "array", "minItems": 1,
                "description": "随文字回应发送的媒体或附件，原样交给发送层；不放普通发言文本，仅用于 mode=text。",
                "items": {"oneOf": [
                    *MEDIA_BUBBLE_SCHEMA,
                ]},
            },
        },
        "required": ["intent", "reference"],
        "additionalProperties": False,
        "allOf": [{"if": {"properties": {"mode": {"const": "voice"}}, "required": ["mode"]},
                   "then": {"not": {"anyOf": [{"required": ["attachments"]}, {"required": ["reply_to_message_id"]}]}}}],
    },
}
