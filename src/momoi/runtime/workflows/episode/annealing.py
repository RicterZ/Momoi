import asyncio
import logging
from typing import Any

from ....observability.events import log_event
from ....models import ToolCall
from ....storage import EPISODE_CONSOLIDATION_BATCH_SIZE, estimate_tokens
from ...agent import AgentWorkflow
from ...turn_support import EPISODE_SUMMARY_SYSTEM_PROMPT
from .contracts import EPISODE_SUMMARY_FINISH_SPEC
from .rendering import render_episode_annealing_request

logger = logging.getLogger(__name__)


class EpisodeAnnealingWorkflow:
    async def _run_episode_annealing_once(
        self, *, allow_partial_consolidation: bool = False
    ) -> bool:
        minimum = (
            1
            if allow_partial_consolidation
            else EPISODE_CONSOLIDATION_BATCH_SIZE
        )
        pending_count = self.store.episode_consolidation_pending_count()
        log_event(
            logger,
            logging.DEBUG,
            "episode_maintenance_selection",
            stage="episode_anneal",
            allow_partial_consolidation=allow_partial_consolidation,
            consolidation_minimum=minimum,
            consolidation_pending=pending_count,
        )
        consolidation = self.store.claim_episode_consolidation_candidate(
            minimum=minimum
        )
        if consolidation is not None:
            log_event(
                logger,
                logging.DEBUG,
                "episode_consolidation_selected",
                stage="episode_consolidate",
                turns=len(consolidation.get("turns") or []),
                context_turns=len(consolidation.get("context_turns") or []),
            )
            archived = await self._consolidate_episode_turns(consolidation)
            remaining = self.store.claim_episode_consolidation_candidate(
                minimum=minimum
            )
            if remaining is not None and archived:
                return True
        candidate = self.store.claim_episode_annealing_candidate(
            self.config.episode_raw_tail_turns, self._episode_raw_token_budget()
        )
        if candidate is None:
            log_event(
                logger,
                logging.DEBUG,
                "episode_maintenance_no_candidate",
                stage="episode_anneal",
                consolidation_pending=self.store.episode_consolidation_pending_count(),
            )
            return False
        episode = candidate["episode"]
        episode_id = str(episode["id"])
        through_ordinal = int(candidate["through_ordinal"])
        log_event(
            logger,
            logging.DEBUG,
            "episode_anneal_selected",
            stage="episode_anneal",
            episode_id=episode_id,
            through_ordinal=through_ordinal,
        )
        turn_id = self._turn_id("episode-anneal", episode_id, through_ordinal)
        state = self.store.begin_turn(
            turn_id,
            "episode_anneal",
            [f"episode-anneal:{episode_id}:{through_ordinal}"],
        )
        if state in {"completed", "cancelled"}:
            self.store.release_episode_annealing(episode_id, failed=False)
            return False
        try:
            completed = await self._anneal_episode_history(
                turn_id,
                candidate=candidate,
                max_seconds=self.config.episode_annealing.max_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.store.record_turn_failure(turn_id, type(error).__name__)
            raise
        if completed:
            self.store.complete_background_turn(turn_id)
        return completed

    async def _anneal_episode_history(
        self,
        turn_id: str,
        *,
        candidate: dict[str, object] | None = None,
        max_seconds: float | None = None,
    ) -> bool:
        candidate = candidate or self.store.claim_episode_annealing_candidate(
            self.config.episode_raw_tail_turns, self._episode_raw_token_budget()
        )
        if candidate is None:
            return False
        episode = candidate["episode"]
        episode_id = str(episode["id"])
        user_prompt = render_episode_annealing_request(episode, candidate["messages"])
        request = [{"role": "user", "content": user_prompt}]
        workflow_complete = False
        workflow_result: dict[str, object] | None = None

        async def execute_tool(call: ToolCall) -> dict[str, Any]:
            nonlocal workflow_complete, workflow_result
            try:
                working_summary = self.store.finish_episode_annealing(
                    episode_id,
                    int(candidate["through_ordinal"]),
                    call.arguments.get("claims", []),
                    narrative_summary=str(
                        call.arguments.get("narrative_summary") or ""
                    ),
                    emotional_context=call.arguments.get("emotional_context"),
                    outcomes=call.arguments.get("outcomes"),
                )
            except (TypeError, ValueError) as error:
                return {
                    "ok": False,
                    "error": "invalid_episode_summary",
                    "message": str(error),
                }
            workflow_complete = True
            workflow_result = {
                "ok": True,
                "working_summary": working_summary,
            }
            return {
                "ok": True,
                "state": "completed",
                "summary_tokens": estimate_tokens(working_summary),
            }

        workflow = AgentWorkflow(
            stage="episode_anneal",
            tool_names=frozenset({"episode_summary_finish"}),
            execute_tool=execute_tool,
            is_complete=lambda: workflow_complete,
            completion_result=lambda: workflow_result,
            no_tool_correction=(
                "[Trusted runtime protocol error. Plain assistant text is not stored. "
                "Call episode_summary_finish with the complete evidence-backed result.]"
            ),
        )
        try:
            completion = self._run_agent_workflow(
                EPISODE_SUMMARY_SYSTEM_PROMPT,
                request,
                [EPISODE_SUMMARY_FINISH_SPEC],
                turn_id=turn_id,
                workflow=workflow,
            )
            result = (
                await asyncio.wait_for(completion, timeout=max_seconds)
                if max_seconds is not None
                else await completion
            )
            if not isinstance(result, dict) or not workflow_complete:
                raise RuntimeError("episode summary ended before completion")
            working_summary = str(result.get("working_summary") or "")
            log_event(
                logger,
                logging.DEBUG,
                "episode_anneal_complete",
                stage="episode_anneal",
                turn_id=turn_id,
                episode_id=episode_id,
                through_ordinal=candidate["through_ordinal"],
                summary_tokens=estimate_tokens(working_summary),
            )
            return True
        except asyncio.CancelledError:
            self.store.release_episode_annealing(episode_id, failed=False)
            raise
        except Exception:
            self.store.release_episode_annealing(episode_id)
            raise
