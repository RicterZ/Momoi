"""Minimal immediate Plan tool surface."""

PLAN_CREATE = {
    "name": "plan_create",
    "description": "Create a finite immediate plan for a multi-step owner request (reading several sources, sending results, writing chapters). Not a scheduled goal. Save the original requirements in request. Then call plan_start and end_turn to hand execution to the step driver. Do not execute its steps in this owner Turn. If only a proposal was requested, leave it as a draft.",
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
PLAN_START = {
    "name": "plan_start", "description": "Start a draft plan. The pre-start conversation is frozen as step context. Returns immediately; end this owner Turn so the worker can execute it. Repeated starts do not duplicate execution. No extra owner confirmation is needed when execution was requested.",
    "input_schema": {"type": "object", "properties": {"plan_id": {"type": "string", "minLength": 1}}, "required": ["plan_id"], "additionalProperties": False},
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
PLAN_TOOLS = [PLAN_CREATE, PLAN_START]
