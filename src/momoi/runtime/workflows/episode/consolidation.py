import asyncio
import copy
import logging
from typing import Any

from ....observability.events import log_event
from ....models import ToolCall
from ...agent import AgentWorkflow
from ...transcript.maintenance import maintenance_transcript
from ...turn_support import EPISODE_CONSOLIDATION_SYSTEM_PROMPT
from .contracts import (
    EPISODE_CLASSIFY_TURNS_SPEC,
    EPISODE_CONSOLIDATION_FINISH_SPEC,
)
from .rendering import render_episode_consolidation_request

logger = logging.getLogger(__name__)


class EpisodeConsolidationWorkflow:
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
        marks = self.store.episode_consolidation_decision_marks(turn_ids)
        # A deferred decision is provisional: when this Turn is selected again
        # after newer owner context, it must be eligible for replacement. A
        # defer produced during this batch is already covered until the next
        # selection, so track those separately from the reprocessable set.
        reprocessable_source = {
            turn_id for turn_id, mark in zip(turn_ids, marks, strict=True) if mark > 0
        }
        resolved: set[str] = set()
        turn_id = self._turn_id(
            "episode-consolidate",
            *turn_ids,
            f"through:{through}",
            *(f"decided:{mark}" for mark in marks),
        )
        state = self.store.begin_turn(
            turn_id,
            "episode_consolidate",
            [f"episode-consolidate:{value}" for value in turn_ids],
        )
        if state in {"completed", "cancelled"}:
            return False
        source_turn_ids = [*turn_ids, *[str(item["turn_id"]) for item in context_items]]
        request, labels = maintenance_transcript(
            self.store,
            self.store.conversation_messages_for_turns(source_turn_ids),
            source_turn_ids,
        )
        source_ids = {labels[value]: value for value in source_turn_ids}
        reprocessable = {
            labels[value] for value in reprocessable_source if value in labels
        }
        referenced = copy.deepcopy(candidate)
        for key in ("turns", "context_turns"):
            for item in referenced.get(key, []):
                item["turn_id"] = labels[str(item["turn_id"])]
        turn_ids = [labels[value] for value in turn_ids]
        user_prompt = render_episode_consolidation_request(referenced)
        request.append({"role": "user", "content": user_prompt})
        candidate_episode_ids = [
            str(episode["id"])
            for episode in candidate["candidate_episodes"]
            if isinstance(episode, dict) and episode.get("id")
        ]
        allow_ignore_latest = bool(context_items)
        workflow_complete = False
        workflow_result: dict[str, object] | None = None

        def remaining() -> list[str]:
            pending_source = set(self.store.episode_consolidation_remaining(
                [source_ids[value] for value in turn_ids]
            ))
            pending_source.update(
                source_ids[value]
                for value in reprocessable
                if value not in resolved
            )
            return [
                value
                for value in turn_ids
                if source_ids[value] in pending_source
            ]

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
                stored_decisions = [
                    {**decision, "turn_ids": [source_ids[value] for value in decision["turn_ids"]]}
                    for decision in decisions
                ]
                linked, deferred = self.store.apply_episode_consolidation(
                    [source_ids[value] for value in selected],
                    stored_decisions,
                    candidate_episode_ids,
                    allow_ignore_latest=True,
                )
                resolved.update(selected)
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
            preserve_transcript=True,
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
