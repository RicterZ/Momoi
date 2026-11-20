"""Persona-free background Worker delegated by the Planner.

One Worker runs at a time, concurrently with Planner turns. Progress reports
and the final result wake a Planner turn, which decides what to tell the user.
"""
import asyncio
import logging
import math
from collections import deque
from datetime import datetime
from importlib.resources import files
from time import time
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import escape, quoteattr

from ...mcp.prompt import MCP_TOOL_POLICY
from ...memory.metadata import WORKER_SCOPE
from ...models import TurnDraft
from ...observability.events import log_event
from ...tools.contracts.skills import SKILL_TOOL_POLICY
from ..agent import AgentWorkflow, TurnExecutionSpec
from ..tool_contracts.subagent import WORKER_SUBAGENT_TOOL_NAMES
from ..turn_support import live_prompt

logger = logging.getLogger("momoi.runtime.turns")

SUBAGENT_MAX_ROUNDS = 50
SUBAGENT_SOFT_LIMIT_ROUNDS = 30
SUBAGENT_IDLE_POLL_SECONDS = 5
SUBAGENT_RESEND_MAX_AGE_SECONDS = 24 * 3600
SUBAGENT_MESSAGE_LIMIT = 5
SUBAGENT_MESSAGE_WINDOW_SECONDS = 60
WORKER_PRACTICE_LIMIT = 3
WORKER_PRACTICE_TOKENS = 1200
# Shadow mode: matches are only logged until their effect on real tasks has been judged.
WORKER_PRACTICE_INJECT = False


def worker_practices_text(rows) -> str:
    if not rows:
        return ""
    items = []
    for row in rows:
        pitfalls = "".join(f"<pitfall>{escape(item)}</pitfall>" for item in row["meta"].get("pitfalls", []))
        items.append(f"<practice id={quoteattr(str(row['id']))}>{escape(str(row['content']))}{pitfalls}</practice>")
    return ("\n<practices>\n以往同类任务里验证过的做法，适用时照做；与任务要求冲突时以任务为准。\n"
            + "\n".join(items) + "\n</practices>")

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

    def message_running_subagent(self, task_id: str, message: str) -> dict[str, Any]:
        inbox = self._subagent_inboxes.get(task_id)
        if inbox is None:
            return {"ok": False, "error": "task_not_running",
                    "message": "Worker 没有在运行这个任务，消息未发送。"}
        now = time()
        sent = self._subagent_message_times.setdefault(task_id, deque())
        while sent and now - sent[0] >= SUBAGENT_MESSAGE_WINDOW_SECONDS:
            sent.popleft()
        if len(sent) >= SUBAGENT_MESSAGE_LIMIT:
            return {"ok": False, "error": "subagent_message_rate_limited", "message": (
                f"{SUBAGENT_MESSAGE_WINDOW_SECONDS} 秒内已给这个 Worker 发了 {SUBAGENT_MESSAGE_LIMIT} 条消息，"
                "先等它回答或汇报。"
            )}
        sent.append(now)
        message_id = f"m_{uuid4().hex[:10]}"
        inbox.append({"id": message_id, "message": message.strip()})
        log_event(logger, logging.INFO, "subagent_message_queued", task_id=task_id, message_id=message_id)
        return {"ok": True, "task_id": task_id, "message_id": message_id, "state": "queued",
                "message": "已排队，Worker 下一步之前会看到；回答时运行时会唤醒你。"}

    def _subagent_reply_channel(self, name: str) -> str:
        channel = self.channels.get(name)
        fallback = getattr(channel, "dialogue_channel", None)
        if fallback in self.channels and fallback != name and callable(getattr(channel, "session_valid", None)):
            if not channel.session_valid(channel.routing_context()):
                return fallback
        return name

    def _resend_subagent_wakeups(self) -> None:
        for task in self.store.unnotified_subagent_tasks(max_age_seconds=SUBAGENT_RESEND_MAX_AGE_SECONDS):
            log_event(logger, logging.INFO, "subagent_wake_resent", task_id=task["id"])
            self._wake_planner_for_subagent(task, task.get("turn_id") or task["source_turn_id"], "finished", "")

    async def _subagent_worker(self, stop: asyncio.Event) -> None:
        self._resend_subagent_wakeups()
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
        inbox: list[dict[str, str]] = []
        self._subagent_inboxes[task_id] = inbox
        delivered: dict[str, str] = {}

        async def handle(call):
            nonlocal finished
            args = call.arguments
            if call.name == "subagent_report":
                progress = args.get("progress")
                if not isinstance(progress, str) or not progress.strip() or len(progress) > 1000:
                    return {"ok": False, "error": "invalid_progress"}
                reply_to = args.get("reply_to")
                if reply_to is not None and reply_to not in delivered:
                    return {"ok": False, "error": "unknown_message_id"}
                try:
                    self.store.add_subagent_report(task_id, progress.strip())
                except ValueError:
                    return {"ok": False, "error": "task_not_running"}
                if reply_to is not None:
                    self._wake_planner_for_subagent(
                        task, turn_id, "reply", progress.strip(), question=delivered.pop(reply_to),
                    )
                else:
                    self._wake_planner_for_subagent(task, turn_id, "progress", progress.strip())
                return {"ok": True, "message": "已转达派发者，继续执行任务。"}
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
            while inbox:
                item = inbox.pop(0)
                delivered[item["id"]] = item["message"]
                live_messages.append({"role": "user", "content": (
                    f"<planner_message id=\"{item['id']}\">\n{item['message']}\n</planner_message>\n"
                    "派发者发来的消息。需要回答时用 subagent_report 回答，reply_to 填这条消息的 id；"
                    "然后按调整后的要求继续任务。"
                )})
                log_event(logger, logging.INFO, "subagent_message_delivered",
                          task_id=task_id, message_id=item["id"])

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
        practices = self.memory.triggered(
            [task["title"], task["task"]], limit=WORKER_PRACTICE_LIMIT,
            token_budget=WORKER_PRACTICE_TOKENS, scope=WORKER_SCOPE,
        )
        messages = [{"role": "user", "content": (
            f"<task id=\"{task_id}\" title=\"{task['title']}\" current_time=\"{now}\" "
            f"workdir=\"{self.tool_executor.artifact_root.resolve()}\">\n"
            f"{task['task']}\n</task>" + (worker_practices_text(practices) if WORKER_PRACTICE_INJECT else "")
        )}]
        log_event(logger, logging.INFO, "subagent_started", task_id=task_id, turn_id=turn_id,
                  title=task["title"])
        if practices:
            log_event(logger, logging.INFO, "worker_practices_matched", task_id=task_id, turn_id=turn_id,
                      injected=WORKER_PRACTICE_INJECT, practices=[row["id"] for row in practices],
                      keys=[row["key"] for row in practices])
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
        finally:
            self._subagent_inboxes.pop(task_id, None)
            self._subagent_message_times.pop(task_id, None)
        unread = [item["message"] for item in inbox]
        current = self.store.subagent_task(task_id)
        log_event(logger, logging.INFO, "subagent_finished", task_id=task_id, turn_id=turn_id,
                  status=current["status"] if current else None)
        if current is not None and current["status"] != "cancelled":
            self._wake_planner_for_subagent(task, turn_id, "finished", "", unread=unread)

    def _wake_planner_for_subagent(
        self, task: dict[str, Any], worker_turn_id: str, kind: str, progress: str,
        *, question: str = "", unread: list[str] | None = None,
    ) -> None:
        current = self.store.subagent_task(task["id"]) or task
        if kind == "finished":
            lines = [
                f"Worker 任务「{current['title']}」（task_id={current['id']}）已结束，状态：{current['status']}。",
                "结果：" + str(current.get("summary") or ""),
            ]
            if current.get("output_refs"):
                lines.append("结果引用：" + "、".join(current["output_refs"]))
            if unread:
                lines.append("Worker 结束前没有读到你发的消息：" + "；".join(unread))
            lines.append("根据结果决定如何告知用户；需要继续时可以再派发 Worker。")
        elif kind == "reply":
            lines = [
                f"Worker 任务「{current['title']}」（task_id={current['id']}）回答了你的消息，仍在执行。",
                f"你的消息：{question}",
                f"Worker 回答：{progress}",
                "根据回答决定是否告知用户或继续给 Worker 发消息。",
            ]
        else:
            lines = [
                f"Worker 任务「{current['title']}」（task_id={current['id']}）仍在执行，阶段汇报：{progress}",
                "判断是否值得告诉用户这条进展；没有必要时不回复，Worker 会继续执行。",
            ]
        heartbeat_turn_id = (
            current["source_turn_id"]
            if kind == "finished" and self.store.heartbeat_awaiting_worker(current["source_turn_id"]) else ""
        )
        if heartbeat_turn_id:
            lines.append(
                "这项任务是心跳时派发的，那次心跳的活动等结果回来才记录：处理完后调用 heartbeat_activity，"
                "写明这次心跳实际做了什么、结果和下次检查时间（next_check_minutes 为 "
                f"{max(1, math.ceil(self.config.heartbeat.min_interval_seconds / 60))}-"
                f"{min(1440, math.floor(self.config.heartbeat.max_interval_seconds / 60))} 分钟）。"
            )
        prompt = "\n".join(lines)
        turn_id = self._turn_id("subagent-report", current["id"], kind, len(current.get("reports") or []))
        if kind == "finished":
            row = self.store._db.execute("SELECT state FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row is not None and row["state"] in {"completed", "cancelled", "needs_reconciliation"}:
                self.store.mark_subagent_notified(current["id"])
                return

        async def wake():
            try:
                reply = await self._request_webhook_turn(
                    prompt, turn_id, channel=self._subagent_reply_channel(current["channel"]),
                    parent_turn_id=worker_turn_id, contract="subagent",
                    heartbeat_turn_id=heartbeat_turn_id,
                )
                try:
                    target = self._subagent_reply_channel(current["channel"]) if current["channel"] else ""
                    if self.store.commit_subagent_report_reply(turn_id, reply, target, heartbeat_turn_id):
                        self.outbox_changed.set()
                    if kind == "finished":
                        self.store.mark_subagent_notified(current["id"])
                finally:
                    self._webhook_turn_settled(turn_id)
            except Exception as error:
                log_event(logger, logging.WARNING, "subagent_report_failed", task_id=current["id"],
                          kind=kind, error_type=type(error).__name__)

        self._subagent_wakeups.add(asyncio.create_task(wake()))
        self._subagent_wakeups = {item for item in self._subagent_wakeups if not item.done()}
