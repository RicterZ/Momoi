import asyncio
import logging
import time
from typing import Any
from types import SimpleNamespace

from ...observability.events import log_event
from ...models import ToolCall
from ...storage.reflection.reflection_values import reflection_window
from ..agent import AgentWorkflow
from ..context.presentation import heartbeat_self_state_lines
from ..parsing import parse_reflection_finish
from ..tool_contracts.reflection import REFLECTION_FINISH_SPEC
from ..transcript.reflection import reflection_evidence, reflection_messages, reflection_transcript
from ..turn_support import (
    REFLECTION_PROMPT_PATH,
    REFLECTION_SYSTEM_PROMPT,
    live_prompt as _live_prompt,
    pack_user_context as _pack_user_context,
)
from .reflection_retrieval import REFLECTION_RETRIEVAL_SPECS, ReflectionRetrieval

logger = logging.getLogger("momoi.runtime.turns")


class ReflectionWorkflow:
    async def _prepare_reflection_episodes(
        self, local_date: str, *, at: str | None = None, end_at: float | None = None,
    ) -> None:
        if not self.config.episode_annealing.enabled:
            return
        window = reflection_window(
            local_date, at or self.config.reflection.at, self.store.timezone, end_at=end_at,
        )
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
                    self.config.episode_unsummarized_tail_turns, self._episode_raw_token_budget(),
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
        reflection = self.store.reflection(local_date)
        # scheduled_at is the trigger time: the fixed boundary for scheduled runs,
        # or the command time for manual runs. Queueing/retries must not extend it.
        end_at = float(reflection["scheduled_at"]) if reflection is not None else None
        await self._prepare_reflection_episodes(local_date, at=reflection_at, end_at=end_at)
        source = self.store.reflection_source(
            local_date,
            at=reflection_at,
            end_at=end_at,
        )
        window = (source["start_at"], source["end_at"])
        rows = self.store.conversation_messages_for_turns(None, window=window)
        chat = reflection_messages(self.store, rows)
        transcript_messages = reflection_transcript(self.store, chat)
        evidence_passages, owner_passages = reflection_evidence(chat)
        cutoff = min(window[1], time.time())
        current_input = _pack_user_context(
            ("self_state", heartbeat_self_state_lines(
                self.store.self_state_context(affinity_enabled=True),
            ) if self.config.affinity_enabled else ""),
            (
                "reflection_period",
                f"复盘时段：{self.store.context_timestamp(window[0])} 至 "
                f"{self.store.context_timestamp(window[1])}（不含结束时刻）\n"
                f"材料截取至：{self.store.context_timestamp(cutoff)}",
            ),
        )
        system = self._workflow_system(_live_prompt(REFLECTION_PROMPT_PATH, REFLECTION_SYSTEM_PROMPT))
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
        settlement_done = not self.config.affinity_enabled or bool(self.store._db.execute(
            "SELECT 1 FROM turn_journal WHERE (item_type='affinity_settlement' AND json_extract(payload_json, '$.local_date')=?) "
            "OR (item_type='affinity_review' AND json_extract(payload_json, '$.settlement_date')=?) LIMIT 1",
            (local_date, local_date)).fetchone())
        from ..tool_contracts.affinity import AFFINITY_TOOLS
        tools = [*(AFFINITY_TOOLS if self.config.affinity_enabled else []), REFLECTION_FINISH_SPEC, *REFLECTION_RETRIEVAL_SPECS]
        retrieval = ReflectionRetrieval(self.store, self.memory_tools, window[1])
        workflow_complete = False
        workflow_result: dict[str, object] | None = None

        async def execute_tool(call: ToolCall) -> dict[str, Any]:
            nonlocal workflow_complete, workflow_result, settlement_done
            if call.name == "affinity_change":
                if settlement_done:
                    return {"ok": False, "error": "affinity_already_settled", "message": "本次复盘已结算，不重复调整。"}
                from ..agent.affinity import review_affinity
                evidence_rows = [row for row in rows if row.get("role") == "user" or
                                 (row.get("role") == "assistant" and row.get("delivery_state") == "delivered")]
                result = await review_affinity(self.config, self.store, self.provider, call,
                    SimpleNamespace(turn_id=turn_id, affinity_dialogue=evidence_rows,
                                    affinity_settlement=True, affinity_settlement_date=local_date, delivery_channel=self.channel))
                settlement_done = True
                self.store.append_turn_journal(turn_id, "affinity_settlement", {"local_date": local_date, **result}, trust="runtime")
                return result
            if call.name == "reflection_finish" and not settlement_done:
                return {"ok": False, "error": "affinity_settlement_required", "message": "请先调用 affinity_change 完成本日关系结算，可以保持不变；不得为了结算而强行增减。"}
            if call.name != "reflection_finish":
                return await retrieval.execute(call)
            validation_errors = []
            decision, error = parse_reflection_finish(
                call.arguments,
                evidence_passages,
                owner_passages,
                owner_passages,
                errors=validation_errors,
            )
            if decision is None:
                return {
                    "ok": False,
                    "error": error or "invalid_reflection_finish",
                    "message": "Correct the indicated field and resubmit the complete result; keep unaffected observations.",
                    "details": validation_errors,
                }
            self.store.commit_reflection(
                local_date,
                turn_id,
                decision["summary"],
                decision["memories"],
            )
            self.agenda_changed.set()
            if self.config.episode_annealing.enabled:
                self._episode_annealing_dirty = True
            workflow_complete = True
            workflow_result = {
                "ok": True,
                "memories": len(decision["memories"]),
            }
            return {"ok": True, "state": "completed", **workflow_result}

        workflow = AgentWorkflow(
            stage="reflection",
            preserve_transcript=True,
            tool_names=frozenset(str(tool["name"]) for tool in tools),
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
        )
        # Worker review has its own Turn and failure path; it never fails the diary.
        await self._complete_worker_review(local_date, window[1])
