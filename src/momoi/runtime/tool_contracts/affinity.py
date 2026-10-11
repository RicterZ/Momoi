"""Planner requests; the runtime supplies actual dialogue to an independent reviewer."""
AFFINITY_TOOLS = [
    {
        "name": name,
        "description": f"申请{direction}好感度；提供理由和可核实的证据。系统附上最近最多 40 条真实对话（至少 10 条，包含角色），经独立 LLM 审批允许后修改数值；拒绝时返回原因。",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000, "description": "关系为何发生变化，以及幅度的依据。"},
                "evidence": {"type": "string", "minLength": 1, "maxLength": 4000, "description": "引用实际消息或已核实事件；说明来源，不编造证据。"},
            },
            "required": ["reason", "evidence"],
        },
    }
    for name, direction in (("affinity_increase", "增加"), ("affinity_decrease", "降低"))
]
