import asyncio
import logging
from typing import Any

from ....logging_context import log_event
from ....models import ToolCall
from ....storage import estimate_tokens
from ...turn_support import (
    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
    EPISODE_SUMMARY_SYSTEM_PROMPT,
)
from .rendering import (
    render_episode_annealing_request,
    render_episode_consolidation_request,
)
from ...agent import AgentWorkflow
from .contracts import (
    EPISODE_CLASSIFY_TURNS_SPEC,
    EPISODE_CONSOLIDATION_FINISH_SPEC,
    EPISODE_SUMMARY_FINISH_SPEC,
)

logger = logging.getLogger(__name__)


class EpisodeWorkflow:
    async def _run_episode_annealing_once(
        self, *, allow_partial_consolidation: bool = False
    ) -> bool:
        minimum = 1 if allow_partial_consolidation else 6
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

    async def _consolidate_episode_turns(self, candidate: dict[str, object]) -> bool:
        turns = candidate["turns"]
        if not isinstance(turns, list) or not turns:
            return False
        turn_ids = [str(turn["turn_id"]) for turn in turns]
        context_turns = candidate.get("context_turns")
        context_items = context_turns if isinstance(context_turns, list) else []
        through = ""
        if context_items and isinstance(context_items[-1], dict):
            through = str(context_items[-1].get("turn_id") or "")
        turn_id = self._turn_id("episode-consolidate", *turn_ids, f"through:{through}")
        state = self.store.begin_turn(
            turn_id,
            "episode_consolidate",
            [f"episode-consolidate:{value}" for value in turn_ids],
        )
        if state in {"completed", "cancelled"}:
            return False
        user_prompt = render_episode_consolidation_request(candidate)
        request = [{"role": "user", "content": user_prompt}]
        candidate_episode_ids = [
            str(episode["id"])
            for episode in candidate["candidate_episodes"]
            if isinstance(episode, dict) and episode.get("id")
        ]
        allow_ignore_latest = bool(context_items)
        workflow_complete = False
        workflow_result: dict[str, object] | None = None

        def remaining() -> list[str]:
            return self.store.episode_consolidation_remaining(turn_ids)

        async def execute_tool(call: ToolCall) -> dict[str, Any]:
            nonlocal workflow_complete, workflow_result
            if call.name == "episode_consolidation_finish":
                pending = remaining()
                if pending:
                    return {
                        "ok": False,
                        "error": "incomplete_consolidation_turn_coverage",
                        "covered_turn_ids": [
                            value for value in turn_ids if value not in pending
                        ],
                        "remaining_turn_ids": pending,
                    }
                self.store.complete_background_turn(turn_id)
                workflow_complete = True
                workflow_result = {"ok": True, "covered_turn_ids": turn_ids}
                return {"ok": True, "state": "completed", **workflow_result}

            decisions = call.arguments.get("decisions")
            if not isinstance(decisions, list) or not decisions:
                return {
                    "ok": False,
                    "error": "invalid_consolidation_decisions",
                    "remaining_turn_ids": remaining(),
                }
            decision_turn_ids: list[str] = []
            for decision in decisions:
                if not isinstance(decision, dict):
                    return {
                        "ok": False,
                        "error": "invalid_consolidation_decision",
                        "remaining_turn_ids": remaining(),
                    }
                raw_ids = decision.get("turn_ids")
                if not isinstance(raw_ids, list) or any(
                    not isinstance(value, str) for value in raw_ids
                ):
                    return {
                        "ok": False,
                        "error": "invalid_consolidation_turn_coverage",
                        "remaining_turn_ids": remaining(),
                    }
                decision_turn_ids.extend(raw_ids)
                if decision.get("action") == "defer" and raw_ids != [turn_ids[-1]]:
                    return {
                        "ok": False,
                        "error": "only_latest_consolidation_turn_may_defer",
                        "remaining_turn_ids": remaining(),
                    }
                if (
                    decision.get("action") == "ignore"
                    and turn_ids[-1] in raw_ids
                    and not allow_ignore_latest
                ):
                    return {
                        "ok": False,
                        "error": "latest_consolidation_turn_may_not_be_ignored",
                        "remaining_turn_ids": remaining(),
                    }
            pending = remaining()
            selected = [value for value in turn_ids if value in decision_turn_ids]
            if (
                not selected
                or len(decision_turn_ids) != len(set(decision_turn_ids))
                or set(decision_turn_ids) != set(selected)
                or not set(selected) <= set(pending)
            ):
                return {
                    "ok": False,
                    "error": "invalid_or_already_covered_turn_subset",
                    "remaining_turn_ids": pending,
                }
            try:
                linked, deferred = self.store.apply_episode_consolidation(
                    selected,
                    decisions,
                    candidate_episode_ids,
                    allow_ignore_latest=True,
                )
            except ValueError as error:
                return {
                    "ok": False,
                    "error": "invalid_consolidation_decisions",
                    "message": str(error),
                    "remaining_turn_ids": remaining(),
                }
            pending = remaining()
            return {
                "ok": True,
                "state": "applied",
                "linked": linked,
                "deferred": deferred,
                "covered_turn_ids": selected,
                "remaining_turn_ids": pending,
            }

        workflow = AgentWorkflow(
            stage="episode_consolidate",
            tool_names=frozenset(
                {"episode_classify_turns", "episode_consolidation_finish"}
            ),
            execute_tool=execute_tool,
            is_complete=lambda: workflow_complete,
            completion_result=lambda: workflow_result,
            no_tool_correction=(
                "[Trusted runtime protocol error. Plain assistant text is not stored. "
                "Call episode_classify_turns for remaining Turns, or call "
                "episode_consolidation_finish after every Turn is durably covered.]"
            ),
        )
        try:
            result = await asyncio.wait_for(
                self._run_agent_workflow(
                    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
                    request,
                    [
                        EPISODE_CLASSIFY_TURNS_SPEC,
                        EPISODE_CONSOLIDATION_FINISH_SPEC,
                    ],
                    turn_id=turn_id,
                    workflow=workflow,
                ),
                timeout=self.config.episode_annealing.max_seconds,
            )
            if not isinstance(result, dict) or not workflow_complete:
                raise RuntimeError("episode consolidation ended before completion")
            log_event(
                logger,
                logging.DEBUG,
                "episode_consolidation_complete",
                stage="episode_consolidate",
                turn_id=turn_id,
                turns=len(turn_ids),
                remaining=len(remaining()),
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.store.record_turn_failure(turn_id, type(error).__name__)
            raise

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
