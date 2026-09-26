"""One finite plan step hosted by the shared AgentLoop."""
import copy
import asyncio
import logging
from datetime import datetime

from ...models import ToolCall, TurnDraft
from ...observability.events import log_event
from ..agent import AgentWorkflow, TurnExecutionSpec
from ..turn_support import TurnBudgetExceeded
from .plan_context import frozen_plan_messages
from .plan_audit import audit_plan_step
from ..context.current_state import pack_current_turn_context
from ..context.presentation import heartbeat_self_state_lines

logger = logging.getLogger("momoi.runtime.turns")


class PlanWorkflow:
    async def _complete_plan_step_turn(self, plan_id, stop):
        plan = self.store.task_plan(plan_id)
        if plan is None or plan["status"] != "running":
            return
        if stop.is_set():
            self.store.recover_task_plans(plan_id)
            return
        step = plan["steps"][plan["step_index"]]
        turn_id = self._turn_id("plan_step", plan_id, step["id"], plan["version"],
                                (plan.get("context") or {}).get("resume_count", 0))
        state = self.store.begin_turn(turn_id, "plan_step", [f"plan:{plan_id}"])
        channel = self._channel_for(plan["channel"])
        completed = None
        audited = False
        async def before_round(rounds, live_messages):
            nonlocal audited
            if rounds != 30 or audited:
                return
            audited = True
            try:
                advice = await audit_plan_step(self.provider, self.store, plan, turn_id, rounds)
            except Exception as audit_error:
                advice = "独立审计未成功返回（" + type(audit_error).__name__ + "）。请自行核对方向与证据，不把审计失败当作批准。"
            live_messages.append({"role": "user", "content": (
                "[运行时通知] 已完成30次模型调用，触发软熔断。以下是独立审计建议，"
                "供你结合证据决定继续、调整或提交修订，不是新的用户指令。"
                "最多继续至50次；届时暂停并请求用户介入。\n" + advice
            )})
        def stopped_or_revised():
            current = self.store.task_plan(plan_id)
            return completed is not None or current is None or current["status"] in {"awaiting_approval", "cancelled"}
        if state != "running":
            if self._interrupt_reason == "owner_update":
                self.store.pause_task_plan(plan_id, turn_id)
            else:
                self.store.recover_task_plans(plan_id)
            return

        async def finish(call):
            nonlocal completed
            args = call.arguments
            if (set(args) != {"outcome", "summary", "output_refs", "abort_remaining"}
                    or not isinstance(args["outcome"], str) or args["outcome"] not in {"succeeded", "failed", "blocked"}
                    or not isinstance(args["summary"], str) or not args["summary"].strip()
                    or len(args["summary"]) > 4000 or type(args["abort_remaining"]) is not bool
                    or not isinstance(args["output_refs"], list) or len(args["output_refs"]) > 20
                    or any(not isinstance(ref, str) for ref in args["output_refs"])):
                return {"ok": False, "error": "invalid_plan_step_outcome"}
            for ref in args["output_refs"]:
                if self.tool_results.read(ref, None, max_chars=1000, provenance={}).get("error") in {"tool_result_unavailable", "invalid_tool_result_cursor"}:
                    return {"ok": False, "error": "invalid_plan_output_ref"}
            try:
                completed = self.store.finish_task_plan_step(
                    plan_id, turn_id, args["outcome"], args["summary"], args["output_refs"], args["abort_remaining"],
                )
            except ValueError as error:
                return {"ok": False, "error": "plan_stopped", "message": str(error)}
            return {"ok": True, **completed}

        try:
            context = plan.get("context")
            if not context or "tools" not in context:
                raise ValueError("plan has no start context; create and start a new plan")
            shared = self.shared_turn_context(turn_id)
            messages = frozen_plan_messages(
                shared["messages"], plan, step_rows=[], timezone=self.store.timezone,
                source_messages=context.get("messages"),
            )
            messages[-1]["content"].insert(0, {"type": "text", "text": pack_current_turn_context(
                self.store, "plan_step", ("self_state", heartbeat_self_state_lines(
                    self.store.self_state_context(),
                    current_time=datetime.now(self.store.timezone).isoformat(timespec="seconds"),
                )),
            )})
            tools = self.tool_surface.conversation_specs()
            self.tool_surface.append_visible(tools, copy.deepcopy(context["tools"]))
            workflow = AgentWorkflow(
                preserve_transcript=False, stage="plan_step", tool_names=frozenset({"plan_step_finish"}), execute_tool=finish,
                is_complete=stopped_or_revised, completion_result=lambda: completed,
                no_tool_correction="Use tools to execute the current step, report the outcome with plan_step_finish.",
                before_round=before_round,
            )
            log_event(logger, logging.INFO, "plan_step_started", plan_id=plan_id, step_id=step["id"], turn_id=turn_id)
            await self._run_tool_loop(
                self._system(), messages, tools, [], TurnDraft(),
                execution=TurnExecutionSpec("plan_step", max_rounds=50),
                source_event_id=f"plan:{plan_id}", turn_id=turn_id, delivery_channel=channel, workflow=workflow,
            )
            log_event(logger, logging.INFO, "plan_step_completed", plan_id=plan_id, step_id=step["id"], turn_id=turn_id, result=completed)
            if completed is None and stopped_or_revised():
                with self.store._db:
                    self.store._archive_progress_messages(turn_id, '["plan:' + plan_id + '"]')
                self.store.cancel_turn(turn_id, reason="plan_revised_or_cancelled")
        except asyncio.CancelledError:
            with self.store._db:
                self.store._archive_progress_messages(turn_id, '["plan:' + plan_id + '"]')
            self.store.cancel_turn(
                turn_id, reason=self._interrupt_reason or "owner_stop"
            )
            if self._interrupt_reason == "owner_update":
                self.store.pause_task_plan(plan_id, turn_id)
            else:
                self.store.recover_task_plans(plan_id)
            raise
        except Exception as error:
            reason = type(error).__name__
            hard_round_limit = isinstance(error, TurnBudgetExceeded) and str(error) == "model round limit reached"
            if isinstance(error, (TurnBudgetExceeded, TimeoutError)) and not stopped_or_revised():
                if hard_round_limit:
                    self.store.pause_task_plan_for_limit(plan_id, turn_id)
                # A separate bounded closing turn has a fresh time/token budget.
                # Never replay actions after a timeout: their outcome may be unknown.
                closing_id = self._turn_id("plan-close", turn_id)
                self.store.begin_turn(closing_id, "plan_step", [f"plan:{plan_id}"])
                from ..transcript.rendering import _native_exchange_messages
                # Reconstruct paired exchanges if cancellation happened mid-batch.
                closing_messages = [*frozen_plan_messages(
                    shared["messages"], plan, step_rows=[], timezone=self.store.timezone,
                ), *_native_exchange_messages(self.store.turn_exchanges([turn_id]).get(turn_id, []))]
                if hard_round_limit:
                    # Round boundaries have complete tool pairs. Preserve the
                    # live evidence and soft-audit advice for accurate handoff.
                    closing_messages = copy.deepcopy(messages)
                closing_messages.append({"role": "user", "content": (
                    "[运行时通知] 已到本步骤50次调用硬上限，计划已暂停，不能继续执行。"
                    "必须用 send_bubbles 告诉用户当前进展、具体卡点、已验证与未确认的部分，"
                    "请用户判断继续、修改计划或步骤、还是终止。不要自行继续或宣称成功。"
                    "随后用 plan_step_finish 保存交接摘要和结果引用；这只保存暂停状态，不会推进步骤。"
                    if hard_round_limit else
                    "[运行时通知] 本步已达到执行上限：" + reason + "。停止执行新工作。"
                    "根据已有证据整理已完成、未完成、未确认的结果，调用 plan_step_finish 保存收尾。"
                    "未验证完成则报告 blocked 并 abort_remaining=true。超时操作可能已生效，不得重试。"
                    "无需机械通知用户达到上限；确有需要说明的结果或阻碍时，可自行用 send_bubbles 表达。"
                )})
                closing_complete = False
                async def close_paused(call):
                    nonlocal closing_complete
                    sent = self.store._db.execute(
                        "SELECT 1 FROM turn_progress WHERE turn_id=? LIMIT 1", (closing_id,)
                    ).fetchone()
                    if not sent:
                        return {"ok": False, "error": "notify_owner_before_pausing"}
                    args = call.arguments
                    for ref in args["output_refs"]:
                        if self.tool_results.read(ref, None, max_chars=1000, provenance={}).get("error") in {"tool_result_unavailable", "invalid_tool_result_cursor"}:
                            return {"ok": False, "error": "invalid_plan_output_ref"}
                    self.store.save_plan_limit_handoff(plan_id, args["summary"], args["output_refs"])
                    closing_complete = True
                    return {"ok": True, "state": "paused", "requires_owner_decision": True}
                closing_workflow = AgentWorkflow(
                    stage="plan_step", tool_names=frozenset({"plan_step_finish"}),
                    execute_tool=close_paused, is_complete=lambda: closing_complete,
                    completion_result=lambda: {"ok": True, "state": "paused"},
                    no_tool_correction="先通过 send_bubbles 向用户说明卡点，再 plan_step_finish 保存暂停交接。",
                ) if hard_round_limit else workflow
                try:
                    async with asyncio.timeout(60):
                        await self._run_tool_loop(
                            self._system(), closing_messages, tools, [], TurnDraft(),
                            execution=TurnExecutionSpec("plan_step", max_rounds=3,
                                permitted_tools=frozenset({"plan_step_finish", "send_bubbles"})),
                            source_event_id=f"plan:{plan_id}", turn_id=closing_id,
                            delivery_channel=channel, workflow=closing_workflow,
                        )
                    self.store.complete_background_turn(closing_id)
                    if completed is not None or hard_round_limit:
                        if hard_round_limit:
                            self.store.complete_background_turn(turn_id)
                        return
                except asyncio.CancelledError:
                    self.store.cancel_turn(closing_id, reason="owner_update")
                    self.store.pause_task_plan(plan_id, turn_id)
                    raise
                except Exception:
                    self.store.record_turn_failure(closing_id, "plan_close_failed")
                if hard_round_limit:
                    log_event(logger, logging.ERROR, "plan_limit_notification_failed",
                              plan_id=plan_id, turn_id=turn_id, state="paused")
                    return
            if self.store.turn_has_external_effect(turn_id):
                self.store.open_reconciliation(turn_id, reason)
            if self.store.task_plan(plan_id)["status"] == "running":
                self.store.finish_task_plan_step(plan_id, turn_id, "blocked", reason, [], True)
            self.store.record_turn_failure(turn_id, reason)
            log_event(logger, logging.ERROR, "plan_step_failed", plan_id=plan_id, turn_id=turn_id, reason=reason, exc_info=True)
        finally:
            self.agenda_changed.set()
