"""Optional built-in phone status; transport control is not an LLM loop."""
QQ_CALL_STATUS_SPEC = {
    "name": "qq_call_status",
    "description": "查询 QQ 语音电话的配置就绪状态和当前通话状态。不会拨号、接听或挂断。",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
