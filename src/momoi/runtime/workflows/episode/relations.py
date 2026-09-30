"""Model led, evidence backed relations between Episodes."""

from __future__ import annotations

import asyncio
import logging
import uuid

from ....models import ToolCall
from ....observability.events import log_event
from ...agent import AgentWorkflow
from ...agent.runtime_tools import recall_owner_context
from ...tool_contracts.context import RECALL_TOOL_SPEC
from .relation_contracts import FINISH_SPEC, SYSTEM, render_source

logger = logging.getLogger(__name__)


class EpisodeRelationWorkflow:
    async def _run_episode_relation_once(self) -> bool:
        episode = self.store.claim_episode_relation_candidate()
        if episode is None:
            return False
        episode_id = str(episode["id"])
        ordinal = int(episode["summarized_through_ordinal"])
        turn_id = self._turn_id("episode-relation", episode_id, ordinal)
        state = self.store.begin_turn(
            turn_id, "episode_relation", [f"episode-relation:{episode_id}:{ordinal}"],
        )
        if state == "cancelled":
            turn_id = self._turn_id("episode-relation", episode_id, ordinal, uuid.uuid4().hex)
            state = self.store.begin_turn(
                turn_id, "episode_relation", [f"episode-relation:{episode_id}:{ordinal}"],
            )
        if state != "running":
            self.store.release_episode_relation_candidate(episode_id)
            return False
        try:
            await asyncio.wait_for(
                self._build_episode_relations(episode, ordinal, turn_id),
                timeout=self.config.episode_annealing.max_seconds,
            )
            self.store.complete_background_turn(turn_id)
            return True
        except asyncio.CancelledError:
            self.store.cancel_turn(turn_id, reason="episode_relation_interrupted")
            self.store.release_episode_relation_candidate(episode_id, failed=False)
            raise
        except Exception as error:
            self.store.cancel_turn(turn_id, reason=type(error).__name__)
            self.store.release_episode_relation_candidate(episode_id)
            raise

    async def _build_episode_relations(self, episode, ordinal, turn_id):
        episode_id = str(episode["id"])
        source_messages = self.store.episode_messages(
            episode_id, 30000, before_ordinal=ordinal + 1
        )
        request = [{"role": "user", "content": render_source(episode, source_messages)}]
        recalled: dict[str, dict[str, object]] = {}
        search_count = 0
        completed = False

        async def submit_relation_context(events, context_turn_id, arguments):
            return await self.submit_owner_context(
                events, context_turn_id, arguments, model_selection=False,
                current_episode_ids=(episode_id,),
            )

        async def execute_tool(call: ToolCall):
            nonlocal completed, search_count
            if call.name == "recall":
                result = await recall_owner_context(
                    call, current_events=[], turn_id=turn_id,
                    submit_context=submit_relation_context,
                )
                if result.get("ok"):
                    if any(unit.get("recall_mode") == "search"
                           for unit in call.arguments.get("units", [])):
                        search_count += 1
                    for record in result.get("episodes", []):
                        recalled[str(record["id"])] = record
                return result

            decisions = call.arguments.get("relations")
            if not isinstance(decisions, list) or len(decisions) > 4:
                return {"ok": False, "error": "invalid_relations"}
            if not search_count:
                return {"ok": False, "error": "recall_required"}
            try:
                self.store.finish_episode_relations(
                    episode_id, ordinal, decisions, set(recalled),
                    evidence_records={**recalled, episode_id: {
                        "title": episode["title"], "summary": episode["narrative_summary"],
                        "conversation": [{"content": m["content"]} for m in source_messages],
                    }},
                )
            except (TypeError, KeyError, ValueError) as error:
                return {"ok": False, "error": "invalid_relations", "message": str(error)}
            completed = True
            log_event(logger, logging.INFO, "episode_relations_reviewed",
                      stage="episode_relation", turn_id=turn_id, episode_id=episode_id,
                      search_count=search_count, candidate_count=len(recalled),
                      relation_count=len(decisions))
            return {"ok": True, "state": "completed", "relation_count": len(decisions)}

        workflow = AgentWorkflow(
            stage="episode_relation",
            tool_names=frozenset({"recall", "episode_relation_finish"}),
            execute_tool=execute_tool,
            is_complete=lambda: completed,
            completion_result=lambda: {"ok": True} if completed else None,
            no_tool_correction=(
                "Call recall to search for older related Episodes, then call "
                "episode_relation_finish with supported relations or an empty list."
            ),
        )
        await self._run_agent_workflow(
            SYSTEM, request, [RECALL_TOOL_SPEC, FINISH_SPEC], turn_id=turn_id, workflow=workflow,
        )
        if not completed:
            raise RuntimeError("episode relation workflow ended without completion")
