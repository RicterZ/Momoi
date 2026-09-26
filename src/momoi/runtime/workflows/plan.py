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
            )
            log_event(logger, logging.INFO, "plan_step_started", plan_id=plan_id, step_id=step["id"], turn_id=turn_id)
            async with asyncio.timeout(300):
                await self._run_tool_loop(
                    self._system(), messages, tools, [], TurnDraft(),
                    execution=TurnExecutionSpec("plan_step", max_rounds=24),
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
            explanation = "达到本步执行上限" if isinstance(error, (TurnBudgetExceeded, TimeoutError)) else "执行出现异常"
            if self.store.turn_has_external_effect(turn_id):
                self.store.open_reconciliation(turn_id, reason)
            if self.store.task_plan(plan_id)["status"] == "running":
                # Reuse delivery/outbox, rather than dropping errors or retrying the whole step.
                self.bubble_delivery.dispatch(
                    ToolCall("plan-failure", "send_bubbles", {"bubbles": [f"计划在第 {step['id']} 步停止了：{explanation}，后续步骤没有继续。"]}),
                    turn_id=turn_id, stage="plan_step", round_number=0, delivery_channel=channel,
                    heartbeat_turn=False, reply_followup_turn=False, heartbeat_owner_event_revision=None,
                    previous_tool_name=None, previous_bubbles=None, previous_channel="",
                )
                self.store.finish_task_plan_step(plan_id, turn_id, "blocked", reason, [], True)
            self.store.record_turn_failure(turn_id, reason)
            log_event(logger, logging.ERROR, "plan_step_failed", plan_id=plan_id, turn_id=turn_id, reason=reason, exc_info=True)
        finally:
            self.agenda_changed.set()
