"""Persona-free background Worker delegated by the Planner.

One Worker runs at a time, concurrently with Planner turns. Progress reports
and the final result wake a Planner turn, which decides what to tell the user.
"""
import asyncio
import logging
from datetime import datetime
from importlib.resources import files
from time import time
from typing import Any

from ...mcp.prompt import MCP_TOOL_POLICY
from ...models import TurnDraft
from ...observability.events import log_event
from ...tools.contracts.skills import SKILL_TOOL_POLICY
from ..agent import AgentWorkflow, TurnExecutionSpec
from ..tool_contracts.subagent import WORKER_SUBAGENT_TOOL_NAMES
from ..turn_support import live_prompt

logger = logging.getLogger("momoi.runtime.turns")

SUBAGENT_MAX_ROUNDS = 50
SUBAGENT_SOFT_LIMIT_ROUNDS = 30
SUBAGENT_REPORT_WAKE_SECONDS = 60
SUBAGENT_IDLE_POLL_SECONDS = 5


class SubagentWorkflow:
    def _subagent_system(self) -> list[dict[str, Any]]:
        default = files("momoi").joinpath("prompts/subagent.md").read_text(encoding="utf-8")
        path = self.config.soul_prompt_path.parent / "SUBAGENT.md" if self.config.soul_prompt_path else None
        prompt = live_prompt(path, default, optional=True) if path else default
        policies = [SKILL_TOOL_POLICY.strip()]
        if self.mcp.tool_specs:
            policies.append(MCP_TOOL_POLICY.strip())
        text = "\n\n".join(filter(None, (
            prompt.strip(), "\n\n".join(policies), self.tool_surface.tool_index("worker"),
        )))
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    def notify_subagents_changed(self) -> None:
        self._subagent_changed.set()

    def cancel_running_subagent(self, task_id: str) -> None:
        running = self._running_subagent
        if running is not None and running[0] == task_id and not running[1].done():
            running[1].cancel()

    async def _subagent_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.config.budget.enabled and self.budget_guard.status()["blocked"]:
                try:
                    await asyncio.wait_for(stop.wait(), 1)
                except TimeoutError:
                    pass
                continue
            self._subagent_changed.clear()
            task = self.store.claim_subagent_task()
            if task is None:
                try:
                    await asyncio.wait_for(self._subagent_changed.wait(), SUBAGENT_IDLE_POLL_SECONDS)
                except TimeoutError:
                    pass
                continue
            run = asyncio.create_task(self._run_subagent_task(task))
            self._running_subagent = (task["id"], run)
            try:
                await asyncio.wait({run})
            except asyncio.CancelledError:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                raise
            finally:
                self._running_subagent = None
            if not run.cancelled() and run.exception() is not None:
                log_event(logger, logging.ERROR, "subagent_worker_error", task_id=task["id"],
                          error_type=type(run.exception()).__name__)

    async def _run_subagent_task(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        turn_id = self._turn_id("subagent", task_id)
        parent = task["source_turn_id"]
        if self.store._db.execute("SELECT 1 FROM turns WHERE id=?", (parent,)).fetchone() is None:
            parent = None
        self.store.begin_turn(turn_id, "subagent", [f"subagent:{task_id}"], parent_turn_id=parent)
        self.store.attach_subagent_turn(task_id, turn_id)
        channel = self._channel_for(task["channel"])
        finished: dict[str, Any] | None = None
        last_wake = 0.0

        async def handle(call):
            nonlocal finished, last_wake
            args = call.arguments
            if call.name == "subagent_report":
                progress = args.get("progress")
                if not isinstance(progress, str) or not progress.strip() or len(progress) > 1000:
                    return {"ok": False, "error": "invalid_progress"}
                try:
                    self.store.add_subagent_report(task_id, progress.strip())
                except ValueError:
                    return {"ok": False, "error": "task_not_running"}
                if time() - last_wake >= SUBAGENT_REPORT_WAKE_SECONDS:
                    last_wake = time()
                    self._wake_planner_for_subagent(task, turn_id, "progress", progress.strip())
                return {"ok": True, "message": "已记录并转达派发者，继续执行任务。"}
            outcome, summary, refs = args.get("outcome"), args.get("summary"), args.get("output_refs")
            if (outcome not in {"succeeded", "failed", "blocked"}
                    or not isinstance(summary, str) or not summary.strip() or len(summary) > 6000
                    or not isinstance(refs, list) or len(refs) > 20
                    or any(not isinstance(ref, str) for ref in refs)):
                return {"ok": False, "error": "invalid_subagent_outcome"}
            for ref in refs:
                if self.tool_results.read(ref, None, max_chars=1000, provenance={}).get("error") in {
                    "tool_result_unavailable", "invalid_tool_result_cursor",
                }:
                    return {"ok": False, "error": "invalid_output_ref", "ref": ref}
            record = self.store.finish_subagent_task(task_id, outcome, summary.strip(), refs)
            finished = record or self.store.subagent_task(task_id)
            return {"ok": True, "status": finished["status"]}

        async def before_round(rounds, live_messages):
            if rounds == SUBAGENT_SOFT_LIMIT_ROUNDS:
                live_messages.append({"role": "user", "content": (
                    f"[运行时通知] 已用 {rounds} 轮模型调用，最多 {SUBAGENT_MAX_ROUNDS} 轮。"
                    "检查方向是否正确，尽快收尾；无法完成时用 subagent_finish 说明已完成和未完成的部分。"
                )})

        def stopped():
            current = self.store.subagent_task(task_id)
            return finished is not None or current is None or current["status"] != "running"

        workflow = AgentWorkflow(
            stage="subagent", tool_names=WORKER_SUBAGENT_TOOL_NAMES, execute_tool=handle,
            is_complete=stopped, completion_result=lambda: finished,
            no_tool_correction="继续用工具执行任务；完成或无法完成时调用 subagent_finish 提交结果。",
            before_round=before_round,
        )
        now = datetime.now(self.store.timezone).isoformat(timespec="seconds")
        messages = [{"role": "user", "content": (
            f"<task id=\"{task_id}\" title=\"{task['title']}\" current_time=\"{now}\">\n"
            f"{task['task']}\n</task>"
        )}]
        log_event(logger, logging.INFO, "subagent_started", task_id=task_id, turn_id=turn_id,
                  title=task["title"])
        try:
            await self._run_tool_loop(
                self._subagent_system(), messages, self.tool_surface.worker_specs(), [], TurnDraft(),
                execution=TurnExecutionSpec("subagent", max_rounds=SUBAGENT_MAX_ROUNDS),
                source_event_id=f"subagent:{task_id}", turn_id=turn_id,
                delivery_channel=channel, workflow=workflow,
            )
        except asyncio.CancelledError:
            self.store.cancel_turn(turn_id, reason="subagent_cancelled")
            self.store.finish_subagent_task(task_id, "failed", "Worker 被中断，可能已有部分操作生效。", [])
            log_event(logger, logging.INFO, "subagent_cancelled", task_id=task_id, turn_id=turn_id)
            raise
        except Exception as error:
            reason = type(error).__name__ + (f": {error}" if str(error) else "")
            self.store.record_turn_failure(turn_id, reason)
            reports = (self.store.subagent_task(task_id) or {}).get("reports") or []
            summary = f"Worker 执行异常（{reason[:300]}），任务未完成。"
            if reports:
                summary += "最近进展：" + reports[-1]["progress"]
            finished = self.store.finish_subagent_task(task_id, "failed", summary, []) or finished
            log_event(logger, logging.ERROR, "subagent_failed", task_id=task_id, turn_id=turn_id,
                      reason=reason[:300])
        else:
            self.store.complete_background_turn(turn_id)
            if finished is None:
                finished = self.store.finish_subagent_task(
                    task_id, "failed", "Worker 未提交结果就结束了。", [],
                )
        current = self.store.subagent_task(task_id)
        log_event(logger, logging.INFO, "subagent_finished", task_id=task_id, turn_id=turn_id,
                  status=current["status"] if current else None)
        if finished is not None and finished["status"] != "cancelled":
            self._wake_planner_for_subagent(task, turn_id, "finished", "")

    def _wake_planner_for_subagent(self, task: dict[str, Any], worker_turn_id: str, kind: str, progress: str) -> None:
        current = self.store.subagent_task(task["id"]) or task
        if kind == "finished":
            lines = [
                f"Worker 任务「{current['title']}」（task_id={current['id']}）已结束，状态：{current['status']}。",
                "结果：" + str(current.get("summary") or ""),
            ]
            if current.get("output_refs"):
                lines.append("结果引用：" + "、".join(current["output_refs"]))
            lines.append("根据结果决定如何告知用户；需要继续时可以再派发 Worker。")
        else:
            lines = [
                f"Worker 任务「{current['title']}」（task_id={current['id']}）仍在执行，阶段汇报：{progress}",
                "判断是否值得告诉用户这条进展；没有必要时不回复，Worker 会继续执行。",
            ]
        prompt = "\n".join(lines)
        turn_id = self._turn_id("subagent-report", current["id"], kind, len(current.get("reports") or []))

        async def wake():
            try:
                reply = await self._request_webhook_turn(
                    prompt, turn_id, channel=current["channel"], parent_turn_id=worker_turn_id,
                    contract="subagent",
                )
                if self.store.commit_subagent_report_reply(turn_id, reply, current["channel"] or ""):
                    self.outbox_changed.set()
            except Exception as error:
                log_event(logger, logging.WARNING, "subagent_report_failed", task_id=current["id"],
                          kind=kind, error_type=type(error).__name__)

        self._subagent_wakeups.add(asyncio.create_task(wake()))
        self._subagent_wakeups = {item for item in self._subagent_wakeups if not item.done()}
