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
SUBAGENT_MESSAGE = {
    "name": "subagent_message",
    "description": (
        "给运行中的 Worker 发一条消息：补充信息、调整要求或询问当前进展。"
        "消息在 Worker 下一步之前插入，立即返回，不等待回答；Worker 回答时运行时会唤醒你。"
        "Worker 看不到聊天记录，消息要写清楚，不要直接转述用户原话。要停止任务用 subagent_cancel。"
    ),
    "input_schema": {"type": "object", "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "message": {"type": "string", "minLength": 1, "maxLength": 2000},
    }, "required": ["task_id", "message"], "additionalProperties": False},
}
SUBAGENT_REPORT = {
    "name": "subagent_report",
    "description": (
        "向派发者汇报，每次调用都会立即唤醒派发者。只在派发者或用户需要现在知道时调用，例如需要用户扫码、确认才能继续；普通进展不汇报，最终结果用 subagent_finish。"
        "回答派发者的消息时，reply_to 填该消息的 id。"
    ),
    "input_schema": {"type": "object", "properties": {
        "progress": {"type": "string", "minLength": 1, "maxLength": 1000},
        "reply_to": {"type": "string", "minLength": 1},
    }, "required": ["progress"], "additionalProperties": False},
}
SUBAGENT_BLOCK = {
    "name": "subagent_block",
    "description": (
        "缺少继续任务必需、自己又查不到的信息或用户操作时调用：说明缺什么，任务挂起并立即唤醒派发者。"
        "挂起期间不消耗轮次；派发者回复后返回，回复以 <planner_message> 给出，据此继续原任务。"
        "派发者可能直接取消任务；超时未回复时任务以 blocked 结束。"
    ),
    "input_schema": {"type": "object", "properties": {
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000,
                   "description": "缺什么、为什么需要，以及已经完成的部分。"},
        "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 900,
                            "description": "最长等待秒数，默认 600。"},
    }, "required": ["reason"], "additionalProperties": False},
}
SUBAGENT_FINISH = {
    "name": "subagent_finish",
    "description": (
        "提交任务最终结果并结束。summary 写派发者需要的结论、关键数据和来源，区分已验证和未确认的内容；"
        "需要原文时在 output_refs 附上工具结果的 result_ref。无法完成时用 failed 说明原因；缺少信息、需要派发者补充时用 subagent_block，不要结束。"
    ),
    "input_schema": {"type": "object", "properties": {
        "outcome": {"enum": ["succeeded", "failed"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 6000},
        "output_refs": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
    }, "required": ["outcome", "summary", "output_refs"], "additionalProperties": False},
}

PLANNER_SUBAGENT_TOOLS = [CREATE_SUBAGENT, SUBAGENT_STATUS, SUBAGENT_MESSAGE, SUBAGENT_CANCEL]
WORKER_SUBAGENT_TOOLS = [SUBAGENT_REPORT, SUBAGENT_BLOCK, SUBAGENT_FINISH]
PLANNER_SUBAGENT_TOOL_NAMES = frozenset(spec["name"] for spec in PLANNER_SUBAGENT_TOOLS)
WORKER_SUBAGENT_TOOL_NAMES = frozenset(spec["name"] for spec in WORKER_SUBAGENT_TOOLS)
