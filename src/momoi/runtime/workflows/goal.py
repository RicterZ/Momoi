import asyncio
import logging
from datetime import datetime
from typing import Any

from ...tools.agenda import AGENDA_TOOL_SPECS, AUTONOMOUS_SEND_BUBBLES_SPEC
from ...tools.builtin import BUILTIN_TOOL_SPECS
from ...context_time import context_timestamp
from ...logging_context import log_event, safe_preview
from ...tools.memory import MEMORY_TOOL_SPECS
from ...models import TurnDraft
from ...provider import ProviderError
from ..agent import TurnExecutionSpec
from ..context.rendering import (
    assemble_recent_external_events,
    recall_episode_context,
)
from ..tool_contracts.runtime import AUTONOMOUS_FINISH_SPEC, READ_TOOL_RESULT_SPEC
from ..transcript.building import build_transcript
from ..transcript.rendering import render_messages
from ..turn_support import (
    ExternalToolTurnError,
    GOAL_PROMPT_PATH,
    GOAL_SYSTEM_PROMPT,
    TurnBudgetExceeded,
    context_data_message as _context_data_message,
    live_prompt as _live_prompt,
    pack_user_context as _pack_user_context,
    reconciliation_message as _reconciliation_message,
    turn_tool_names as _turn_tool_names,
)

logger = logging.getLogger("momoi.runtime.turns")


class GoalWorkflow:
    async def _complete_goal_turn(self, goal_id: str, stop: asyncio.Event) -> None:
        goal = self.store.goal(goal_id)
        turn_id = self._turn_id(
            "goal", goal_id, goal.get("next_review_at") if goal else "missing"
        )
        state = self.store.begin_turn(turn_id, "goal", [f"goal:{goal_id}"])
        if state in {"completed", "cancelled"}:
            self.store.release_goal_claim(goal_id)
            return
        if state == "needs_reconciliation":
            self.store.commit_autonomous_turn(
                goal_id,
                TurnDraft(
                    notification_messages=[_reconciliation_message(turn_id)],
                    notification_key="goal.reconciliation",
                    notification_priority="urgent",
                    notification_reason="External action outcome requires owner confirmation.",
                ),
                turn_id=turn_id,
            )
            self.outbox_changed.set()
            self.store.record_turn_failure(
                turn_id, "process_interrupted_after_external_effect"
            )
            return
        if stop.is_set():
            return
        try:
            try:
                await self._complete_goal(goal_id, turn_id)
            except (ExternalToolTurnError, TurnBudgetExceeded, asyncio.CancelledError):
                raise
            except Exception as error:
                if self.store.turn_has_external_effect(turn_id):
                    raise ExternalToolTurnError(type(error).__name__) from error
                raise
            return
        except ExternalToolTurnError:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="goal",
                turn_id=turn_id,
                goal_id=goal_id,
                reason="fatal_error_after_external_tool",
                exc_info=True,
            )
            self.store.open_reconciliation(turn_id, "fatal_error_after_external_tool")
            draft = TurnDraft(
                notification_messages=[_reconciliation_message(turn_id)],
                notification_key="goal.reconciliation",
                notification_priority="urgent",
                notification_reason="External action outcome requires owner confirmation.",
            )
            failure_reason = "fatal_error_after_external_tool"
        except TurnBudgetExceeded as error:
            log_event(
                logger,
                logging.WARNING,
                "turn_failure",
                stage="goal",
                turn_id=turn_id,
                goal_id=goal_id,
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
            )
            draft = TurnDraft(
                notification_messages=[
                    "I paused this task after it reached the per-turn processing limit."
                ],
                notification_key="goal.budget",
                notification_priority="urgent",
                notification_reason="Autonomous turn budget exhausted.",
            )
            failure_reason = type(error).__name__
        except asyncio.CancelledError:
            if self._stop_requested:
                self.store.cancel_turn(turn_id)
            raise
        except ProviderError as error:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="goal",
                turn_id=turn_id,
                goal_id=goal_id,
                layer="provider",
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
            )
            retry_at = self.store.defer_goal_failure(goal_id)
            self.store.record_turn_failure(turn_id, type(error).__name__)
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
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="goal",
                turn_id=turn_id,
                goal_id=goal_id,
                layer="runtime",
                error_type=type(error).__name__,
                exc_info=True,
            )
            retry_at = self.store.defer_goal_failure(goal_id)
            self.store.record_turn_failure(turn_id, type(error).__name__)
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
        self.store.commit_autonomous_turn(goal_id, draft, turn_id=turn_id)
        if draft.notification_messages:
            self.outbox_changed.set()
        self.store.record_turn_failure(turn_id, failure_reason)
        self.agenda_changed.set()

    async def _complete_goal(self, goal_id: str, turn_id: str) -> None:
        goal = self.store.goal(goal_id)
        if goal is None or goal["status"] not in {"active", "waiting"}:
            self.store.release_goal_claim(goal_id)
            return
        now = datetime.now(self.store.timezone).isoformat(timespec="seconds")
        review_at = context_timestamp(goal["next_review_at"], self.store.timezone)
        self_state = self.store.self_state_context()
        memory_query = f"{goal['title']} {goal['next_action']} {goal['latest_result']}"
        memories, learned = self.store.ranked_memory_context(
            memory_query,
            self.config.memory_results,
        )
        recent_memories = self.store.recent_memory_context()
        long_term_memories = self.store.always_memory_context()
        conversation_rows = self._recent_conversation_rows()
        tool_activity = self.store.turn_activity(
            [str(row["turn_id"]) for row in conversation_rows]
        )
        transcript = build_transcript(
            conversation_rows,
            timezone=self.store.timezone,
            tool_activity=tool_activity,
        )
        transcript_messages = render_messages(
            [*transcript.orphaned, *transcript.groups],
            timezone=self.store.timezone,
            tool_activity=tool_activity,
        )
        recent_turn_ids = {
            turn_id
            for group in (*transcript.orphaned, *transcript.groups)
            for turn_id in group.turn_ids
        }
        episodes = recall_episode_context(
            self.store,
            memory_query,
            self.config.summary_results,
            self.config.summary_tokens,
            exclude_turn_ids=recent_turn_ids,
        )
        goal_event = (
            "[Trusted autonomous runtime event generated by Momoi. This is not a new "
            "bubble or authorization from the owner.]\n"
            "Trigger: goal.review\n"
            "Turn identity: Goal review\n"
            f"Current local time: {now}\n"
            f"Goal id: {goal_id}\n"
            f"Goal authority: {goal['authority']}\n"
            f"Title: {goal['title']}\n"
            f"Success criteria: {goal['success_criteria']}\n"
            f"Status: {goal['status']}\n"
            f"Plan: {goal['plan'] or 'none'}\n"
            f"Next action: {goal['next_action']}\n"
            f"Waiting for: {goal['waiting_for'] or 'none'}\n"
            f"Latest result: {goal['latest_result'] or 'none'}\n"
            f"Recurring schedule: {goal['schedule'] or 'none'}\n"
            f"Scheduled review time: {review_at}"
        )
        current_input = _pack_user_context(
            (
                "workflow_contract",
                _live_prompt(GOAL_PROMPT_PATH, GOAL_SYSTEM_PROMPT),
            ),
            ("due_goal", goal_event),
            ("runtime_state", self_state),
            (
                "recent_external_events",
                assemble_recent_external_events(self.store),
            ),
            ("episode_directory", episodes),
            ("recall_memories", memories),
            ("reflection_memories", learned),
        )
        context_message = _context_data_message(
            ("long_term_memories", long_term_memories),
            ("recent_memories", recent_memories),
            required=True,
        )
        assert context_message is not None
        messages: list[dict[str, Any]] = [
            context_message,
            *transcript_messages,
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": current_input,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]
        memory_search = [
            spec for spec in MEMORY_TOOL_SPECS if spec["name"] == "memory_search"
        ]
        agent_owned = goal["authority"] == "agent"
        agenda_specs = (
            [
                spec
                for spec in AGENDA_TOOL_SPECS
                if spec["name"] in {"goal_update", "goal_finish", "goal_cancel"}
            ]
            if agent_owned
            else AGENDA_TOOL_SPECS
        )
        tools = [
            *memory_search,
            *agenda_specs,
            AUTONOMOUS_SEND_BUBBLES_SPEC,
            READ_TOOL_RESULT_SPEC,
            *(
                self.tool_surface.self_directed_specs()
                if agent_owned
                else BUILTIN_TOOL_SPECS
            ),
            *([] if agent_owned else self.mcp.tool_specs),
            AUTONOMOUS_FINISH_SPEC,
        ]
        draft = TurnDraft()
        await self._run_tool_loop(
            self._system(),
            messages,
            tools,
            [],
            draft,
            execution=TurnExecutionSpec(
                "goal",
                goal_id=goal_id,
                allowed_capabilities=(
                    frozenset({"read", "write"}) if agent_owned else None
                ),
                artifact_root=(
                    self.tool_executor.artifact_root if agent_owned else None
                ),
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
            notified=bool(draft.notification_messages),
            tools=_turn_tool_names(draft),
            tool_calls=len(draft.tool_calls),
            goals=len(draft.goals),
            llm=self.store.turn_usage(turn_id),
        )
        self.agenda_changed.set()
