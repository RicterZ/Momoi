"""Research, review, approval, execution and revision of immediate plans."""

PLAN_CREATE = {
    "name": "plan_create",
    "description": "建立计划草稿，不会开始执行。先理解用户意图、目标、约束与验收标准，利用工具调查并核实关键假设，再拆分工作。request 写目标、范围和限制；每个 task 写实施产物、依赖与验证方法。调查尚不充分时继续调查并 plan_update，不要把猜测当成结论。方案成熟后 plan_submit 发给用户审核，获得后续明确同意后才 plan_start。",
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 4000},
        "request": {"type": "string", "minLength": 1, "maxLength": 4000},
        "steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
            "type": "object", "properties": {
                "task": {"type": "string", "minLength": 1, "maxLength": 4000},
                "on_failure": {"enum": ["stop", "continue"]},
            }, "required": ["task", "on_failure"], "additionalProperties": False,
        }},
    }, "required": ["title", "request", "steps"], "additionalProperties": False},
}
PLAN_SUBMIT = {
    "name": "plan_submit", "description": "提交当前版本方案并自动发送 summary 给用户，进入等待审核。summary 用适合用户的语言说明目标、主要工作、范围、取舍、预期结果和待决定事项；evidence 保存调研结论、来源、已排除假设与未解决问题；validation 保存验收方法与成功标准。实施步骤来自计划 steps。不要另发重复 summary。提交后结束本轮等待用户意见，未同意不得实施；用户要求修改时 plan_update 后重新提交。",
    "input_schema": {"type": "object", "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        **{key: {"type": "string", "minLength": 1, "maxLength": 12000}
           for key in ("summary", "evidence", "validation")},
    }, "required": ["plan_id", "version", "summary", "evidence", "validation"], "additionalProperties": False},
}
PLAN_START = {
    "name": "plan_start", "description": "用户明确批准已提交版本后开始执行。approval_quote 必须逐字引用当前用户消息中同意该方案的内容，不能把提问、拒绝或要求修改当作同意。版本变化必须重新提交审核。开始后结束本轮，由后台连续执行，无需逐步征求继续。",
    "input_schema": {"type": "object", "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        "approval_quote": {"type": "string", "minLength": 1},
    }, "required": ["plan_id", "version", "approval_quote"], "additionalProperties": False},
}
PLAN_STEP_FINISH = {
    "name": "plan_step_finish", "description": "End the current step with its outcome. Reference necessary outputs for later steps. Set abort_remaining when a shared prerequisite fails. Runtime advances to the next step; do not call end_turn.",
    "input_schema": {"type": "object", "properties": {
        "outcome": {"enum": ["succeeded", "failed", "blocked"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "output_refs": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "abort_remaining": {"type": "boolean"},
    }, "required": ["outcome", "summary", "output_refs", "abort_remaining"], "additionalProperties": False},
}

PLAN_GET = {"name":"plan_get","description":"读取计划的当前版本、调研依据、用户可见方案、验收方法、审批状态、全部步骤与结果引用。","input_schema":{"type":"object","properties":{"plan_id":{"type":"string"}},"required":["plan_id"],"additionalProperties":False}}
PLAN_UPDATE = {"name":"plan_update","description":"修订当前和后续步骤或目标，保留已完成步骤及证据。允许执行中修订；这会停止旧版本执行并转回草稿，使旧审批失效。调查补全后 plan_submit 重新发给用户审核。步骤内的方法可自行调整，不必为常规实现细节修订方案。","input_schema":{"type":"object","properties":{"plan_id":{"type":"string"},"version":{"type":"integer","minimum":1},"request":{"type":"string","minLength":1,"maxLength":4000},"steps":{"type":"array","minItems":1,"maxItems":12,"items":PLAN_CREATE["input_schema"]["properties"]["steps"]["items"]}},"required":["plan_id","version","steps"],"additionalProperties":False}}
PLAN_CANCEL = {"name":"plan_cancel","description":"Cancel a Plan and its remaining steps.","input_schema":{"type":"object","properties":{"plan_id":{"type":"string"}},"required":["plan_id"],"additionalProperties":False}}
PLAN_RESUME = {"name":"plan_resume","description":"恢复已批准且未修改的计划。50次上限暂停需要用户明确要求继续，提供 owner_feedback，按交接继续而不是重做；普通插话暂停可恢复。修订后的计划必须重新提交并用 plan_start 批准，不能用 resume 绕过审核。 The runtime rejects replay if the interrupted step may have acted externally; in that case inspect the result and create a new Plan for safe remaining work. End this owner Turn after resuming.","input_schema":{"type":"object","properties":{"owner_feedback":{"type":"string","minLength":1,"description":"因50次步骤上限暂停时，必须引用当前用户明确要求继续的原话。"},"plan_id":{"type":"string"},"version":{"type":"integer","minimum":1}},"required":["plan_id","version"],"additionalProperties":False}}
PLAN_TOOLS = [PLAN_CREATE, PLAN_SUBMIT, PLAN_START, PLAN_GET, PLAN_UPDATE, PLAN_CANCEL, PLAN_RESUME]
