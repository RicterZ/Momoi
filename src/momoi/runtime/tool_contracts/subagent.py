"""Planner delegation to the persona-free background Worker."""

CREATE_SUBAGENT = {
    "name": "create_subagent",
    "description": (
        "把需要外部工具的任务派发给后台 Worker（联网搜索、MCP 服务、网页读取、文件、命令、Skill 等）。"
        "立即返回，不等待结果；同一时间只运行一个 Worker，其余排队。"
        "Worker 没有人格、记忆和聊天记录，只能看到 task 文本，所以 task 要写清目标、已知信息、约束和需要交回的结果。"
        "派发后可先 reply 告诉用户正在处理；Worker 汇报进展或完成时，运行时会唤醒你再处理结果。"
    ),
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 80,
                  "description": "任务短标题，用于状态查询和轨迹展示。"},
        "task": {"type": "string", "minLength": 1, "maxLength": 6000,
                 "description": "交给 Worker 的完整任务说明。"},
    }, "required": ["title", "task"], "additionalProperties": False},
}
SUBAGENT_STATUS = {
    "name": "subagent_status",
    "description": "查询 Worker 任务的状态、排队位置、阶段汇报和结果。省略 task_id 时列出当前渠道最近的任务。",
    "input_schema": {"type": "object", "properties": {
        "task_id": {"type": "string", "minLength": 1},
    }, "additionalProperties": False},
}
SUBAGENT_CANCEL = {
    "name": "subagent_cancel",
    "description": "取消排队中或运行中的 Worker 任务。运行中的任务可能已有操作生效，取消后以已汇报内容为准。",
    "input_schema": {"type": "object", "properties": {
        "task_id": {"type": "string", "minLength": 1},
    }, "required": ["task_id"], "additionalProperties": False},
}
SUBAGENT_REPORT = {
    "name": "subagent_report",
    "description": "向派发者汇报阶段性进展，例如已找到关键线索、某个耗时步骤开始或完成。只在有实质进展时调用，不要每步都汇报；最终结果用 subagent_finish。",
    "input_schema": {"type": "object", "properties": {
        "progress": {"type": "string", "minLength": 1, "maxLength": 1000},
    }, "required": ["progress"], "additionalProperties": False},
}
SUBAGENT_FINISH = {
    "name": "subagent_finish",
    "description": (
        "提交任务最终结果并结束。summary 写派发者需要的结论、关键数据和来源，区分已验证和未确认的内容；"
        "需要原文时在 output_refs 附上工具结果的 result_ref。无法完成时用 failed 或 blocked 说明原因和缺少的信息。"
    ),
    "input_schema": {"type": "object", "properties": {
        "outcome": {"enum": ["succeeded", "failed", "blocked"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 6000},
        "output_refs": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
    }, "required": ["outcome", "summary", "output_refs"], "additionalProperties": False},
}

PLANNER_SUBAGENT_TOOLS = [CREATE_SUBAGENT, SUBAGENT_STATUS, SUBAGENT_CANCEL]
WORKER_SUBAGENT_TOOLS = [SUBAGENT_REPORT, SUBAGENT_FINISH]
PLANNER_SUBAGENT_TOOL_NAMES = frozenset(spec["name"] for spec in PLANNER_SUBAGENT_TOOLS)
WORKER_SUBAGENT_TOOL_NAMES = frozenset(spec["name"] for spec in WORKER_SUBAGENT_TOOLS)
