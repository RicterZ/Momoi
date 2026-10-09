"""Research, review, approval, execution and revision of immediate plans."""

PLAN_CREATE = {
    "name": "plan_create",
    "description": '创建计划草稿，不会开始执行。首先理解用户意图、目标、约束与验收标准，利用工具调查并核实关键假设，再拆分工作。request 中撰写目标、范围和限制；每个 task 中撰写实施产物、依赖关系与验证方法。若调查尚不充分，应继续调查并进行 plan_update，切勿将猜测当作结论。方案成熟后通过 plan_submit 提交给用户审核，获得后续明确同意后方可 plan_start。',
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
    "name": "plan_submit", "description": '提交当前版本方案并自动向用户发送 summary，进入等待审核状态。summary 需用适合用户的语言说明目标、主要工作、范围、取舍、预期结果和待决定事项；evidence 保存调研结论、来源、已排除假设与未解决问题；validation 保存验收方法与成功标准。实施步骤来自计划中的 steps。请勿另发重复的 summary。提交后结束本轮并等待用户意见，未经同意不得实施；若用户要求修改，需先 plan_update 再重新提交。',
    "input_schema": {"type": "object", "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        **{key: {"type": "string", "minLength": 1, "maxLength": 12000}
           for key in ("summary", "evidence", "validation")},
    }, "required": ["plan_id", "version", "summary", "evidence", "validation"], "additionalProperties": False},
}
PLAN_START = {
    "name": "plan_start", "description": '在用户明确批准已提交版本后开始执行。approval_quote 必须逐字引用当前用户消息中同意该方案的内容，不得将提问、拒绝或要求修改视为同意。版本变更必须重新提交审核。开始后结束本轮，由后台连续执行，无需逐步征求继续。',
    "input_schema": {"type": "object", "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        "approval_quote": {"type": "string", "minLength": 1},
    }, "required": ["plan_id", "version", "approval_quote"], "additionalProperties": False},
}
PLAN_STEP_FINISH = {
    "name": "plan_step_finish", "description": '以结果结束当前步骤。为后续步骤引用必要的输出。当共享前置条件失败时设置 abort_remaining。运行时将进入下一步。',
    "input_schema": {"type": "object", "properties": {
        "outcome": {"enum": ["succeeded", "failed", "blocked"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "output_refs": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "abort_remaining": {"type": "boolean"},
    }, "required": ["outcome", "summary", "output_refs", "abort_remaining"], "additionalProperties": False},
}

PLAN_GET = {"name":"plan_get","description":"读取计划的当前版本、调研依据、用户可见方案、验收方法、审批状态、全部步骤与结果引用。","input_schema":{"type":"object","properties":{"plan_id":{"type":"string"}},"required":["plan_id"],"additionalProperties":False}}
PLAN_UPDATE = {"name":"plan_update","description":'修订当前及后续步骤或目标，同时保留已完成步骤及其证据。允许在执行中修订；此举将停止旧版本执行并转回草稿，使旧审批失效。调查补全后需通过 plan_submit 重新提交给用户审核。步骤内的方法可自行调整，无需为常规实现细节修订方案。',"input_schema":{"type":"object","properties":{"plan_id":{"type":"string"},"version":{"type":"integer","minimum":1},"request":{"type":"string","minLength":1,"maxLength":4000},"steps":{"type":"array","minItems":1,"maxItems":12,"items":PLAN_CREATE["input_schema"]["properties"]["steps"]["items"]}},"required":["plan_id","version","steps"],"additionalProperties":False}}
PLAN_CANCEL = {"name":"plan_cancel","description":'取消计划及其剩余步骤。',"input_schema":{"type":"object","properties":{"plan_id":{"type":"string"}},"required":["plan_id"],"additionalProperties":False}}
PLAN_RESUME = {"name":"plan_resume","description":'恢复已批准且未修改的计划。若因达到 50 次步骤上限而暂停，必须获得用户明确要求继续的指令，提供 owner_feedback，按交接方式继续而非重做；普通插话导致的暂停可直接恢复。修订后的计划必须重新提交并通过 plan_start 批准，不得用 resume 绕过审核。若中断的步骤可能已对外部产生作用，运行时将拒绝回放；此时应检查结果并为剩余安全工作创建新计划。恢复后结束当前用户回合。',"input_schema":{"type":"object","properties":{"owner_feedback":{"type":"string","minLength":1,"description":'因达到 50 次步骤上限而暂停时，必须引用用户明确要求继续的原话。'},"plan_id":{"type":"string"},"version":{"type":"integer","minimum":1}},"required":["plan_id","version"],"additionalProperties":False}}
PLAN_TOOLS = [PLAN_CREATE, PLAN_SUBMIT, PLAN_START, PLAN_GET, PLAN_UPDATE, PLAN_CANCEL, PLAN_RESUME]
