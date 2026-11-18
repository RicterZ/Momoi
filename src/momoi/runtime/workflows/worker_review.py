"""Review finished Worker tasks after the daily reflection, in a turn of its own."""
import asyncio
import json
import logging
import re
from collections import Counter
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ...observability.events import log_event
from ..agent import AgentWorkflow
from ..turn_support import PROMPT_ROOT, live_prompt

logger = logging.getLogger("momoi.runtime.turns")

PROMPT_PATH = PROMPT_ROOT.joinpath("worker_review.md")
PROMPT = PROMPT_PATH.read_text(encoding="utf-8").strip()

LOOKBACK_SECONDS = 7 * 24 * 3600
MANY_CALLS = 15
LONG_RUNNING_SECONDS = 120
SAME_TOOL_RUN = 8
TOOL_FAILURES = 2
TASK_CHARS = 3000
SUMMARY_CHARS = 2000
ARGUMENT_CHARS = 300
FAILURE_CHARS = 500
SUCCESS_CHARS = 160
MATERIAL_CHARS = 120_000
GENERIC_FAILURE_TOOLS = frozenset({"exec"})

_SECRETS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,]{12,}"),
)

WORKER_REVIEW_FINISH_SPEC = {
    "name": "worker_review_finish",
    "description": "提交每个任务的判断和提炼出的经验并结束本轮；经验只作为候选保存。",
    "input_schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 1000,
                        "description": "一两句话说明本次的整体结论。"},
            "tasks": {
                "type": "array", "minItems": 1, "maxItems": 50,
                "description": "对材料里每个任务的判断，每个任务一条。",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1},
                        "verdict": {"type": "string", "enum": ["skip", "learn"]},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                    "required": ["task_id", "verdict", "reason"],
                },
            },
            "experiences": {
                "type": "array", "maxItems": 5,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": ["execution", "dispatch"]},
                        "applies_to": {"type": "string", "minLength": 1, "maxLength": 200,
                                       "description": "在什么任务情境下适用，用将来任务里可能出现的说法。"},
                        "content": {"type": "string", "minLength": 1, "maxLength": 600,
                                    "description": "直接写做法，最多三句。"},
                        "pitfalls": {"type": "array", "maxItems": 5,
                                     "items": {"type": "string", "minLength": 1, "maxLength": 200}},
                        "evidence": {
                            "type": "array", "minItems": 1, "maxItems": 10,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "properties": {
                                    "task_id": {"type": "string", "minLength": 1},
                                    "steps": {"type": "array", "maxItems": 20, "uniqueItems": True,
                                              "items": {"type": "integer", "minimum": 1}},
                                },
                                "required": ["task_id", "steps"],
                            },
                        },
                    },
                    "required": ["kind", "applies_to", "content", "pitfalls", "evidence"],
                },
            },
        },
        "required": ["summary", "tasks", "experiences"],
    },
}


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(lambda match: (match.group(1) if match.groups() else "") + "<redacted>", text)
    return text


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _signature(step: dict[str, Any]) -> tuple[str, str]:
    return step["name"], str(step["error"] or "tool_failed")


def _duration(task: dict[str, Any]) -> float:
    return max(0.0, float(task.get("finished_at") or 0) - float(task.get("started_at") or 0))


def _longest_run(steps: list[dict[str, Any]]) -> int:
    best = run = 0
    previous = None
    for step in steps:
        run = run + 1 if step["name"] == previous else 1
        previous = step["name"]
        best = max(best, run)
    return best


def recurring_failures(batch: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> dict[tuple[str, str], list[str]]:
    tasks: dict[tuple[str, str], list[str]] = {}
    for task, steps in batch:
        # Shell failures share one generic code whatever the command, so they never form a signature.
        for signature in dict.fromkeys(_signature(step) for step in steps
                                       if step["ok"] is False and step["name"] not in GENERIC_FAILURE_TOOLS):
            tasks.setdefault(signature, []).append(str(task["id"]))
    return {signature: ids for signature, ids in tasks.items() if len(ids) >= 2}


def admission_reasons(task: dict[str, Any], steps: list[dict[str, Any]],
                      recurring: dict[tuple[str, str], list[str]]) -> list[str]:
    """Deterministic signals that a task has something to learn from."""
    failures = [step for step in steps if step["ok"] is False]
    reasons = []
    if task["status"] in {"failed", "blocked"}:
        reasons.append("not_succeeded")
    if len(failures) >= TOOL_FAILURES:
        reasons.append("tool_failures")
    if len(steps) >= MANY_CALLS:
        reasons.append("many_calls")
    if _duration(task) >= LONG_RUNNING_SECONDS:
        reasons.append("long_running")
    if _longest_run(steps) >= SAME_TOOL_RUN:
        reasons.append("same_tool_run")
    if any(_signature(step) in recurring for step in failures):
        reasons.append("recurring_failure")
    return reasons


def _result_text(step: dict[str, Any], load_snapshot) -> str:
    result = step["result"]
    if isinstance(result, dict):
        if not step["ok"] and not result.get("message") and result.get("result_ref") and load_snapshot:
            snapshot = load_snapshot(result["result_ref"])
            try:
                message = json.loads(snapshot).get("message") if snapshot else None
            except (ValueError, AttributeError):
                message = None
            if message:
                result = {**result, "message": message}
        result = {key: value for key, value in result.items()
                  if key not in {"ok", "error", "provenance", "result_ref", "truncated"}}
    return json.dumps(result, ensure_ascii=False, default=str) if result not in (None, {}) else ""


def render_steps(steps: list[dict[str, Any]], load_snapshot=None) -> str:
    lines = []
    for number, step in enumerate(steps, start=1):
        status = "ok" if step["ok"] else f"FAILED {step['error'] or 'tool_failed'}"
        arguments = _clip(redact(json.dumps(step["arguments"], ensure_ascii=False, default=str)), ARGUMENT_CHARS)
        lines.append(f"[{number}] {step['name']} {status}\n  args: {arguments}")
        result = redact(_result_text(step, load_snapshot))
        if result:
            lines.append(f"  result: {_clip(result, SUCCESS_CHARS if step['ok'] else FAILURE_CHARS)}")
    return "\n".join(lines) or "(没有工具调用)"


def render_task(task, steps, reasons, load_snapshot=None) -> str:
    attributes = " ".join(f"{key}={quoteattr(str(value))}" for key, value in (
        ("id", task["id"]), ("title", task["title"]), ("outcome", task["status"]),
        ("calls", len(steps)), ("failures", sum(step["ok"] is False for step in steps)),
        ("duration_seconds", round(_duration(task))), ("admitted_by", ",".join(reasons)),
    ))
    return (
        f"<worker_task {attributes}>\n"
        f"<task>\n{escape(_clip(redact(str(task['task'])), TASK_CHARS))}\n</task>\n"
        f"<steps>\n{escape(render_steps(steps, load_snapshot))}\n</steps>\n"
        f"<summary>\n{escape(_clip(redact(str(task.get('summary') or '')), SUMMARY_CHARS))}\n</summary>\n"
        "</worker_task>"
    )


def worker_review_input(rendered_tasks: list[str], recurring) -> str:
    parts = []
    rows = [f"{name} {error}: {', '.join(ids)}" for (name, error), ids in sorted(recurring.items())]
    parts.append("<recurring_failures>\n" + ("\n".join(rows) or "(无)") + "\n</recurring_failures>")
    parts.extend(rendered_tasks)
    return "\n\n".join(parts)


def parse_worker_review(arguments, admitted) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from ...tools.validation import validate_tool_arguments
    value, error = validate_tool_arguments(
        "worker_review_finish", arguments, WORKER_REVIEW_FINISH_SPEC["input_schema"],
    )
    if error:
        return None, error
    step_counts = {str(task["id"]): len(steps) for task, steps, _ in admitted}
    judged = [item["task_id"] for item in value["tasks"]]
    if sorted(judged) != sorted(step_counts):
        return None, {"ok": False, "error": "tasks_mismatch",
                      "missing": sorted(set(step_counts) - set(judged)),
                      "unknown_or_duplicate": sorted({i for i in judged if i not in step_counts or judged.count(i) > 1}),
                      "message": "tasks 必须对材料里的每个 worker_task 各给出一条判断。"}
    learned = {item["task_id"] for item in value["tasks"] if item["verdict"] == "learn"}
    cited = {evidence["task_id"] for item in value["experiences"] for evidence in item["evidence"]}
    if learned - cited:
        return None, {"ok": False, "error": "learn_without_experience", "task_ids": sorted(learned - cited),
                      "message": "判为 learn 的任务必须被至少一条经验引用；没有经验就判为 skip。"}
    for item in value["experiences"]:
        for evidence in item["evidence"]:
            count = step_counts.get(evidence["task_id"])
            if count is None:
                return None, {"ok": False, "error": "unknown_task", "task_id": evidence["task_id"],
                              "message": "evidence 只能引用本次材料里的 worker_task id。"}
            if any(step > count for step in evidence["steps"]):
                return None, {"ok": False, "error": "unknown_step", "task_id": evidence["task_id"],
                              "message": f"该任务只有 {count} 个步骤。"}
        if not item["content"].strip() or not item["applies_to"].strip():
            return None, {"ok": False, "error": "empty_experience"}
    return value, None


class WorkerReviewWorkflow:
    def _worker_review_batch(self, end_at: float):
        tasks = self.store.unreviewed_worker_tasks(start_at=end_at - LOOKBACK_SECONDS, end_at=end_at)
        steps = [(task, self.store.worker_task_steps(str(task["turn_id"]))) for task in tasks]
        recurring = recurring_failures(steps)
        return [(task, task_steps, admission_reasons(task, task_steps, recurring))
                for task, task_steps in steps], recurring

    async def _complete_worker_review(self, local_date: str, end_at: float) -> None:
        batch, recurring = self._worker_review_batch(end_at)
        if not batch:
            return
        tool_results = getattr(self, "tool_results", None)
        load_snapshot = tool_results.load if tool_results is not None else None
        admitted, rendered, size = [], [], 0
        for task, steps, reasons in batch:
            if not reasons:
                continue
            text = render_task(task, steps, reasons, load_snapshot)
            # Tasks beyond the budget stay unreviewed and lead the next day's batch.
            if rendered and size + len(text) > MATERIAL_CHARS:
                break
            admitted.append((task, steps, reasons))
            rendered.append(text)
            size += len(text)
        last = batch.index(admitted[-1]) if admitted else len(batch) - 1
        decisions = [{"task_id": str(task["id"]), "reasons": reasons} for task, _, reasons in batch[:last + 1]]
        if not admitted:
            self.store.commit_worker_review(local_date, None, decisions, [])
            return
        turn_id = self._turn_id("worker-review", local_date, [decision["task_id"] for decision in decisions])
        state = self.store.begin_turn(turn_id, "worker_review", [f"worker-review:{local_date}"])
        if state != "running":
            return
        material = worker_review_input(rendered, recurring)
        completed = False
        experiences = 0

        async def execute(call):
            nonlocal completed, experiences
            result, error = parse_worker_review(call.arguments, admitted)
            if error:
                return error
            self.store.commit_worker_review(local_date, turn_id, decisions, result["experiences"])
            completed = True
            experiences = len(result["experiences"])
            return {"ok": True, "state": "completed", "experiences": experiences}

        workflow = AgentWorkflow(
            stage="worker_review", preserve_transcript=True,
            tool_names=frozenset({"worker_review_finish"}), execute_tool=execute,
            is_complete=lambda: completed, completion_result=lambda: {"ok": completed},
            no_tool_correction="Call worker_review_finish to submit the Worker review.",
        )
        try:
            await self._run_agent_workflow(
                live_prompt(PROMPT_PATH, PROMPT),
                [{"role": "user", "content": material}],
                [WORKER_REVIEW_FINISH_SPEC], turn_id=turn_id, workflow=workflow,
            )
            if not completed:
                raise RuntimeError("worker_review_incomplete")
        except asyncio.CancelledError:
            self.store.record_turn_failure(turn_id, "worker_review_interrupted")
            raise
        except Exception as error:
            log_event(logger, logging.ERROR, "worker_review_failed", stage="worker_review",
                      turn_id=turn_id, local_date=local_date, error_type=type(error).__name__, exc_info=True)
            self.store.record_turn_failure(turn_id, type(error).__name__)
            return
        log_event(logger, logging.INFO, "turn_complete", stage="worker_review", turn_id=turn_id,
                  local_date=local_date, reviewed=len(decisions), admitted=len(admitted), experiences=experiences)
