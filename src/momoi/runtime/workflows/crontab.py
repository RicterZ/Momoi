import asyncio
import logging

from ...storage.core.timestamps import context_timestamp
from ...observability.events import log_event
from ...models import TurnDraft
from ..agent import TurnExecutionSpec
from ..context.current_state import pack_current_turn_context
from ..context.presentation import due_goal_lines
from ..turn_support import (
    GOAL_PROMPT_PATH,
    GOAL_SYSTEM_PROMPT,
    run_classified_turn,
    live_prompt as _live_prompt,
    reconciliation_message as _reconciliation_message,
    turn_tool_names as _turn_tool_names,
)

logger = logging.getLogger("momoi.runtime.turns")


def _goal_reconciliation_draft(turn_id: str) -> TurnDraft:
    return TurnDraft(
        notification_messages=[_reconciliation_message(turn_id)],
        notification_key="goal.reconciliation",
        notification_priority="urgent",
        notification_reason="External action outcome requires owner confirmation.",
    )


class GoalWorkflow:
    async def _complete_goal_turn(self, goal_id: str, stop: asyncio.Event) -> None:
        goal = self.store.goal(goal_id)
        turn_id = self._turn_id(
            "goal", goal_id, goal.get("next_review_at") if goal else "missing"
        )
        state = self.store.begin_turn(turn_id, "goal", [f"goal:{goal_id}"])
        # A retry that ran ahead of the slot reuses the slot's Turn ID; the slot itself still has to run.
        if (
            state in {"completed", "cancelled"}
            and goal and goal.get("next_review_at") is not None
            and self.store.turn_started_before(turn_id, float(goal["next_review_at"]))
        ):
            turn_id = self._turn_id("goal", goal_id, goal["next_review_at"], "slot")
            state = self.store.begin_turn(turn_id, "goal", [f"goal:{goal_id}"])
        if state in {"completed", "cancelled"}:
            self.store.skip_goal_review(goal_id)
            return
        if state == "needs_reconciliation":
            self.store.commit_autonomous_turn(goal_id, _goal_reconciliation_draft(turn_id), turn_id=turn_id)
            self.outbox_changed.set()
            self.store.record_turn_failure(
                turn_id, "process_interrupted_after_external_effect"
            )
            return
        if stop.is_set():
            return
        try:
            failure = await run_classified_turn(
                self.store, self._complete_goal(goal_id, turn_id),
                stage="goal", turn_id=turn_id, log_fields={"goal_id": goal_id},
            )
        except asyncio.CancelledError:
            if self._stop_requested:
                self.store.cancel_turn(turn_id)
            raise
        if failure is None:
            return
        if failure.kind in {"provider", "runtime"}:
            retry_at = self.store.defer_goal_failure(goal_id)
            self.store.record_turn_failure(turn_id, failure.reason)
            self.agenda_changed.set()
            log_event(
                logger,
                logging.INFO,
                "goal_deferred",
                stage="goal",
                turn_id=turn_id,
                goal_id=goal_id,
                retry_at=retry_at,
            )
            return
        if failure.kind == "external":
            draft = _goal_reconciliation_draft(turn_id)
        else:
            draft = TurnDraft(
                notification_messages=[
                    "这项任务已达到本轮执行上限，已暂停，尚未完成。"
                ],
                notification_key="goal.budget",
                notification_priority="urgent",
                notification_reason="Autonomous turn budget exhausted.",
            )
        self.store.commit_autonomous_turn(goal_id, draft, turn_id=turn_id)
        if draft.notification_messages:
            self.outbox_changed.set()
        self.store.record_turn_failure(turn_id, failure.reason)
        self.agenda_changed.set()

    async def _complete_goal(self, goal_id: str, turn_id: str) -> None:
        goal = self.store.goal(goal_id)
        if goal is None or goal["status"] not in {"active", "waiting"}:
            self.store.release_goal_claim(goal_id)
            return
        review_at = context_timestamp(goal["next_review_at"], self.store.timezone)
        shared = self.shared_turn_context(turn_id)
        recent_goals = self.recent_message_refs(shared, "goal", "G")
        def build_input():
            return pack_current_turn_context(
                self.store, "goal",
                ("scoped_memories", self.store.scoped_memory_context(f"goal:{goal_id}")),
                (
                    "workflow_contract",
                    _live_prompt(GOAL_PROMPT_PATH, GOAL_SYSTEM_PROMPT),
                ),
                ("due_goal", due_goal_lines(self.store.goal(goal_id) or {"id": goal_id, "status": "deleted"}, scheduled_review_at=review_at)),
                self.self_state_section(),
                ("recent_goals", recent_goals),
            )

        current_input = build_input()
        messages = self.autonomous_turn_messages(shared, current_input)
        tools = self.tool_surface.conversation_specs()
        draft = TurnDraft()
        await self._run_tool_loop(
            self._system(planner=True),
            messages,
            tools,
            [],
            draft,
            resume_input=build_input,
            execution=TurnExecutionSpec(
                "goal",
                goal_id=goal_id,
                permitted_tools=self.tool_surface.permitted_names("goal"),
            ),
            source_event_id=f"goal:{goal_id}",
            turn_id=turn_id,
            delivery_channel=self.channel,
        )
        self.store.commit_autonomous_turn(goal_id, draft, turn_id=turn_id)
        log_event(
            logger,
            logging.INFO,
            "turn_complete",
            stage="goal",
            turn_id=turn_id,
            goal_id=goal_id,
            notified=any(
                call["tool"] in {"reply", "send_bubbles", "send_voice"} and call["ok"]
                for call in draft.tool_calls
            ),
            tools=_turn_tool_names(draft),
            tool_calls=len(draft.tool_calls),
            goals=len(draft.goals),
            llm=self.store.turn_usage(turn_id),
        )
        self.agenda_changed.set()
