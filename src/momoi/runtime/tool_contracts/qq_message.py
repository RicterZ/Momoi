"""On-demand control of messages already delivered to QQ."""
QQ_RECALL_MESSAGE_SPEC = {
    "name": "qq_recall_message",
    "description": "撤回机器人已发送到当前 QQ 私聊的消息。省略 outbox_id 时撤回最近一条有投递回执的消息；不能撤回用户的消息。QQ 可能因撤回时限拒绝请求。",
    "input_schema": {
        "type": "object",
        "properties": {"outbox_id": {"type": "integer", "minimum": 1,
                                      "description": "待撤回消息的本地投递编号；省略时取最近一条。"}},
        "additionalProperties": False,
    },
}

QQ_POKE_SPEC = {
    "name": "qq_poke",
    "description": "通过 QQ 私聊主动戳一戳用户，作为轻量互动。仅发送戳一戳动作，不自动附带文字；是否另发文字由你根据上下文判断。",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
