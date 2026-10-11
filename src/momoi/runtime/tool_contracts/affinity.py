"""One relationship decision tool shared by conversation and reflection."""
AFFINITY_TOOLS = [{
    "name": "affinity_change",
    "description": "申请增加或降低好感度，提供理由和证据，经独立审批后调整。仅每日复盘可选择 unchanged 保持不变；此时无需证据，不调用审批模型。",
    "input_schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "direction": {"type": "string", "enum": ["increase", "decrease", "unchanged"]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "evidence": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        "required": ["direction", "reason"],
        "allOf": [{"if": {"properties": {"direction": {"enum": ["increase", "decrease"]}}},
                   "then": {"required": ["evidence"]}}],
    },
}]
