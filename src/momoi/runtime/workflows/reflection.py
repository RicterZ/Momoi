import asyncio
import logging
from typing import Any

from ...observability.events import log_event
from ...models import ToolCall
from ...storage.reflection_values import reflection_window
from ..agent import AgentWorkflow
from ..parsing import parse_reflection_finish
from ..tool_contracts.reflection import REFLECTION_FINISH_SPEC
from ..transcript.maintenance import maintenance_transcript
from ..turn_support import (
    REFLECTION_PROMPT_PATH,
    REFLECTION_SYSTEM_PROMPT,
    live_prompt as _live_prompt,
    pack_user_context as _pack_user_context,
)
from .memory_maintenance import MEMORY_MAINTENANCE_RUN_VERSION

logger = logging.getLogger("momoi.runtime.turns")


class ReflectionWorkflow:
    async def _prepare_reflection_episodes(self, local_date: str, *, at: str | None = None) -> None:
        if not self.config.episode_annealing.enabled:
            return
        window = reflection_window(local_date, at or self.config.reflection.at, self.store.timezone)
        consolidated = summaries = 0
        try:
            async with asyncio.timeout(self.config.episode_annealing.max_seconds):
                active = self._active_annealing
                if active is not None and not active.done():
                    active.cancel("reflection_preparation")
                    await asyncio.gather(active, return_exceptions=True)
                attempted: set[str] = set()
                while candidate := self.store.claim_episode_consolidation_candidate(
                    minimum=1, window=window, exclude_turn_ids=tuple(attempted),
                ):
                    attempted.update(str(turn["turn_id"]) for turn in candidate["turns"])
                    await self._consolidate_episode_turns(candidate)
                    consolidated += len(candidate["turns"])
                skipped: set[str] = set()
                while candidate := self.store.claim_episode_annealing_candidate(
                    self.config.episode_raw_tail_turns, self._episode_raw_token_budget(),
                    window=window, exclude_episode_ids=tuple(skipped),
                ):
                    if not await self._anneal_episode_candidate(candidate):
                        skipped.add(str(candidate["episode"]["id"]))
                        continue
                    summaries += 1
        except Exception as error:
            log_event(
                logger, logging.WARNING, "reflection_episode_preparation_failed",
                stage="reflection", local_date=local_date,
                error_type=type(error).__name__,
            )
        else:
            log_event(
                logger, logging.INFO, "reflection_episodes_prepared",
                stage="reflection", local_date=local_date,
                classified_turns=consolidated, summary_batches=summaries,
            )

    async def _complete_reflection_turn(
        self, local_date: str, stop: asyncio.Event
    ) -> None:
        reflection = self.store.reflection(local_date)
        claimed_at = None if reflection is None else reflection.get("claimed_at")
        turn_id = self._turn_id("reflection", local_date, claimed_at)
        state = self.store.begin_turn(
            turn_id, "reflection", [f"reflection:{local_date}"]
        )
        if state == "completed":
            self.store.restore_completed_reflection_claim(local_date)
            return
        if state == "cancelled":
            self.store.release_reflection(
                local_date, "turn_cancelled", delay_seconds=3600
            )
            return
        if state == "needs_reconciliation" or stop.is_set():
            self.store.release_reflection(
                local_date, "unexpected_reconciliation", delay_seconds=3600
            )
            return
        try:
            await self._complete_reflection(local_date, turn_id)
        except asyncio.CancelledError:
            if self._stop_requested:
                self.store.record_turn_failure(turn_id, "owner_stop")
            raise
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="reflection",
                turn_id=turn_id,
                local_date=local_date,
                error_type=type(error).__name__,
                exc_info=True,
            )
            self.store.record_turn_failure(turn_id, type(error).__name__)
            self.store.release_reflection(local_date, type(error).__name__, 900)
            self.agenda_changed.set()

    async def _complete_reflection(self, local_date: str, turn_id: str) -> None:
        # Keep this run's preparation and evidence on the same window if config reloads.
        reflection_at = self.config.reflection.at
        await self._prepare_reflection_episodes(local_date, at=reflection_at)
        maintenance_turn_id = self._turn_id(
            "memory-maintenance",
            MEMORY_MAINTENANCE_RUN_VERSION,
            "reflection",
            turn_id,
        )
        source = self.store.reflection_source(
            local_date,
            at=reflection_at,
        )
        window = (source["start_at"], source["end_at"])
        rows = self.store.conversation_messages_for_turns(None, window=window)
        transcript_messages, _ = maintenance_transcript(
            self.store, rows, [], window=window,
        )
        raw_record = "\n".join(str(row["content"]) for row in rows)
        query = raw_record[-20000:]
        reflection_evidence = raw_record + "\n" + "\n".join(
            message["content"] if isinstance(message["content"], str) else
            "\n".join(block.get("text", "") for block in message["content"])
            for message in transcript_messages
        )
        owner_source = "\n".join(str(row["content"]) for row in rows if row["role"] == "user")
        knowledge_source = owner_source
        confirmed_memory, learned = self.store.ranked_memory_context(
            query,
            self.config.memory_results,
        )
        open_conversations = self.store.open_conversation_inventory()
        open_episode_ids = {str(item["id"]) for item in open_conversations}
        recent_memories = self.store.recent_memory_context()
        current_input = _pack_user_context(
            (
                "workflow_contract",
                f"Review period: {self.store.context_timestamp(window[0])} "
                f"to {self.store.context_timestamp(window[1])} (end exclusive)\n\n"
                + _live_prompt(REFLECTION_PROMPT_PATH, REFLECTION_SYSTEM_PROMPT),
            ),
            ("open_conversations", self.store.open_conversation_inventory_context()),
            ("recent_memories", recent_memories),
            ("recall_memories", confirmed_memory),
            ("reflection_memories", learned),
            ("mood_timeline", str(source.get("mood_timeline") or "(none)")),
            ("episode_timeline", str(source.get("episode_timeline") or "(none)")),
        )
        system = self._system()
        messages: list[dict[str, Any]] = [
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
            }
        ]
        tools = [REFLECTION_FINISH_SPEC]
        workflow_complete = False
        workflow_result: dict[str, object] | None = None

        async def execute_tool(call: ToolCall) -> dict[str, Any]:
            nonlocal workflow_complete, workflow_result
            decision, error = parse_reflection_finish(
                call.arguments,
                reflection_evidence,
                owner_source,
                knowledge_source,
                open_episode_ids,
            )
            if decision is None:
                return {
                    "ok": False,
                    "error": error or "invalid_reflection_finish",
                    "message": "Correct the reflection result and resubmit it.",
                }
            self.store.commit_reflection(
                local_date,
                turn_id,
                decision["summary"],
                decision["memories"],
                decision["conversation_actions"],
                maintenance_turn_id,
            )
            self._enqueue_memory_maintenance(maintenance_turn_id)
            self.agenda_changed.set()
            if self.config.episode_annealing.enabled:
                self.episode_annealing_requested.set()
            workflow_complete = True
            workflow_result = {
                "ok": True,
                "memories": len(decision["memories"]),
                "conversation_actions": len(decision["conversation_actions"]),
            }
            return {"ok": True, "state": "completed", **workflow_result}

        workflow = AgentWorkflow(
            stage="reflection",
            preserve_transcript=True,
            tool_names=frozenset({"reflection_finish"}),
            execute_tool=execute_tool,
            is_complete=lambda: workflow_complete,
            completion_result=lambda: workflow_result,
            no_tool_correction=(
                "[Trusted runtime protocol error. Plain assistant text is not stored. "
                "Call reflection_finish with the complete retrospective result.]"
            ),
        )
        result = await self._run_agent_workflow(
            system,
            messages,
            tools,
            turn_id=turn_id,
            workflow=workflow,
        )
        if not isinstance(result, dict) or not workflow_complete:
            raise RuntimeError("reflection ended before completion")
        log_event(
            logger,
            logging.INFO,
            "turn_complete",
            stage="reflection",
            turn_id=turn_id,
            local_date=local_date,
            memories=result.get("memories", 0),
            conversation_actions=result.get("conversation_actions", 0),
        )
