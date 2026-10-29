"""On-demand control of messages already delivered to QQ."""
QQ_POKE_SPEC = {
    "name": "qq_poke",
    "description": "真实戳一戳当前 QQ 私聊用户。需要此动作时调用工具，不要输出 qq://poke 或用文字假装已经执行。读取结果后按意图继续发言；只需要动作时不再输出文字。非 QQ 频道调用时不执行动作，静默返回成功。",
    "input_schema": {
        "type": "object", "properties": {}, "additionalProperties": False,
    },
}

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
