"""One finite plan step hosted by the shared AgentLoop."""
import copy
import asyncio
import logging

from ...models import ToolCall, TurnDraft
from ...observability.events import log_event
from ..agent import AgentWorkflow, TurnExecutionSpec
from ..turn_support import TurnBudgetExceeded
from .plan_context import frozen_plan_messages

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
        turn_id = self._turn_id("plan_step", plan_id, step["id"])
        state = self.store.begin_turn(turn_id, "plan_step", [f"plan:{plan_id}"])
        channel = self._channel_for(plan["channel"])
        completed = None
        if state != "running":
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
                if not self.tool_results.read(ref, None, max_chars=1000, provenance={}).get("ok"):
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
            completed_turns = [
                step["turn_id"] for step in plan["steps"][:plan["step_index"]]
            ]
            messages = frozen_plan_messages(
                context["messages"], plan,
                step_rows=self.store.conversation_messages_for_turns(completed_turns),
                timezone=self.store.timezone,
                tool_activity=self.store.turn_activity(completed_turns),
            )
            allowed = {"send_bubbles", "read_tool_result", "tool_enable", "plan_step_finish"}
            allowed.update(str(s["name"]) for s in self.tool_surface.builtin_specs)
            allowed.update(str(s["name"]) for s in self.mcp.tool_specs)
            tools = copy.deepcopy(context["tools"])
            workflow = AgentWorkflow(
                preserve_transcript=True, stage="plan_step", tool_names=frozenset({"plan_step_finish"}), execute_tool=finish,
                is_complete=lambda: completed is not None, completion_result=lambda: completed,
                no_tool_correction="Use tools to execute the current step, report the outcome with plan_step_finish.",
            )
            log_event(logger, logging.INFO, "plan_step_started", plan_id=plan_id, step_id=step["id"], turn_id=turn_id)
            async with asyncio.timeout(300):
                await self._run_tool_loop(
                    context["system"], messages, tools, [], TurnDraft(),
                    execution=TurnExecutionSpec("plan_step", max_rounds=24, permitted_tools=frozenset(allowed)),
                    source_event_id=f"plan:{plan_id}", turn_id=turn_id, delivery_channel=channel, workflow=workflow,
                )
            log_event(logger, logging.INFO, "plan_step_completed", plan_id=plan_id, step_id=step["id"], turn_id=turn_id, result=completed)
        except asyncio.CancelledError:
            with self.store._db:
                self.store._archive_progress_messages(turn_id, '["plan:' + plan_id + '"]')
            self.store.cancel_turn(turn_id)
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
