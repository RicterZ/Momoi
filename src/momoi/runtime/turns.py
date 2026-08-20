import asyncio
import json
import logging
import re
import uuid
from typing import Any

from ..logging_context import log_context, log_event, new_trace_id
from ..storage import estimate_tokens
from .context_planner import CONTEXT_PLAN_TOOL_NAME
from .parsing import (
    parse_messages,
    parse_mood_decision,
    parse_mood_update,
    parse_reflection_finish,
    parse_reply_wait_decision,
    parse_response,
)
from .turn_support import (
    CONTEXT_PLANNER_SYSTEM_PROMPT,
    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
    EPISODE_SUMMARY_SYSTEM_PROMPT,
    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
    MAX_CONSECUTIVE_TOOL_FAILURES,
    STYLE_CARD_SYSTEM_PROMPT,
    ExternalToolTurnError,
    OwnerMessagesChanged,
    TurnBudgetExceeded,
    sections as _sections,
    pack_user_context as _pack_user_context,
    truncate_tool_result_json as _truncate_tool_result_json,
)
from .context_service import ContextService
from .episode_prompt_renderer import (
    render_episode_annealing_request,
    render_episode_consolidation_request,
)
from .prompt_renderer import PromptRenderer
from .tool_execution import ToolExecutionService
from .turn_committer import TurnCommitter
from .turn_orchestrator import TurnOrchestrator

logger = logging.getLogger(__name__)
# Compatibility audit anchors: reason=last_error, raw_plan=raw_plan.


class TurnRunner(
    TurnOrchestrator,
    ContextService,
    ToolExecutionService,
    PromptRenderer,
    TurnCommitter,
):
    _parse_messages = staticmethod(parse_messages)
    _parse_response = staticmethod(parse_response)
    _parse_mood_decision = staticmethod(parse_mood_decision)
    _parse_mood_update = staticmethod(parse_mood_update)
    _parse_reply_wait_decision = staticmethod(parse_reply_wait_decision)
    _parse_reflection_finish = staticmethod(parse_reflection_finish)

    @staticmethod
    def _context_plan_response_text(content: list[dict[str, Any]]) -> str:
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if block.get("type") == "text"
        ).strip()

    @staticmethod
    def _episode_summary_result(text: str) -> dict[str, object]:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError(
                "episode summary provider returned invalid JSON"
            ) from error
        if not isinstance(value, dict) or not isinstance(value.get("claims"), list):
            raise RuntimeError("episode summary provider returned invalid claims")
        if value.get("version") == 1 and set(value) == {"version", "claims"}:
            return {
                "claims": value["claims"],
                "narrative_summary": "",
                "emotional_context": {},
                "outcomes": [],
            }
        if value.get("version") != 2 or set(value) != {
            "version",
            "claims",
            "narrative_summary",
            "emotional_context",
            "outcomes",
        }:
            raise RuntimeError("episode summary provider returned invalid result")
        outcomes = value["outcomes"]
        if isinstance(outcomes, list):
            value["outcomes"] = [
                item["outcome"]
                if isinstance(item, dict)
                and set(item) == {"outcome"}
                and isinstance(item["outcome"], str)
                else item
                for item in outcomes
            ]
        return value


    @staticmethod
    def _turn_id(*parts: object) -> str:
        seed = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
        return uuid.uuid5(uuid.NAMESPACE_URL, f"momoi:{seed}").hex

    async def _run_episode_annealing_once(self) -> bool:
        consolidation = self.store.claim_episode_consolidation_candidate()
        if consolidation is not None:
            archived = await self._consolidate_episode_turns(consolidation)
            remaining = self.store.claim_episode_consolidation_candidate()
            if remaining is not None and archived:
                return True
        candidate = self.store.claim_episode_annealing_candidate(
            self.config.recent_turns, self.config.recent_raw_tokens
        )
        if candidate is None:
            return False
        episode = candidate["episode"]
        episode_id = str(episode["id"])
        through_ordinal = int(candidate["through_ordinal"])
        turn_id = self._turn_id(
            "episode-anneal", episode_id, through_ordinal
        )
        state = self.store.begin_turn(
            turn_id,
            "autonomous",
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

    async def _consolidate_episode_turns(
        self, candidate: dict[str, object]
    ) -> bool:
        turns = candidate["turns"]
        if not isinstance(turns, list) or not turns:
            return False
        turn_ids = [str(turn["turn_id"]) for turn in turns]
        context_turns = candidate.get("context_turns")
        context_items = context_turns if isinstance(context_turns, list) else []
        through = ""
        if context_items and isinstance(context_items[-1], dict):
            through = str(context_items[-1].get("turn_id") or "")
        turn_id = self._turn_id(
            "episode-consolidate", *turn_ids, f"through:{through}"
        )
        state = self.store.begin_turn(
            turn_id,
            "autonomous",
            [f"episode-consolidate:{value}" for value in turn_ids],
        )
        if state in {"completed", "cancelled"}:
            return False
        user_prompt = render_episode_consolidation_request(candidate)
        request = [{"role": "user", "content": user_prompt}]
        try:
            call_id = new_trace_id()
            with log_context(
                stage="episode_consolidate",
                turn_id=turn_id,
                call_id=call_id,
            ):
                response = await asyncio.wait_for(
                    self.provider.complete(
                        EPISODE_CONSOLIDATION_SYSTEM_PROMPT, request, []
                    ),
                    timeout=self.config.episode_annealing.max_seconds,
                )
            text = re.sub(
                r"<think>.*?</think>",
                "",
                self._context_plan_response_text(response.content),
                flags=re.DOTALL,
            ).strip()
            value = json.loads(text)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "decisions"}
                or value["version"] != 1
                or not isinstance(value["decisions"], list)
            ):
                raise RuntimeError("invalid episode consolidation response")
            linked, deferred = self.store.apply_episode_consolidation(
                turn_ids,
                value["decisions"],
                [
                    str(episode["id"])
                    for episode in candidate["candidate_episodes"]
                    if isinstance(episode, dict) and episode.get("id")
                ],
                allow_ignore_latest=bool(context_items),
            )
            action_counts = {
                action: sum(
                    1
                    for decision in value["decisions"]
                    if isinstance(decision, dict)
                    and decision.get("action") == action
                )
                for action in ("defer", "ignore", "continue", "new")
            }
            metrics = response.usage or {}
            self.store.record_turn_usage(
                turn_id,
                int(
                    metrics.get(
                        "input",
                        estimate_tokens(
                            EPISODE_CONSOLIDATION_SYSTEM_PROMPT
                            + user_prompt
                        ),
                    )
                ),
                int(metrics.get("output", estimate_tokens(text))),
            )
            self.store.complete_background_turn(turn_id)
            log_event(
                logger,
                logging.DEBUG,
                "episode_consolidation_complete",
                stage="episode_consolidate",
                turn_id=turn_id,
                call_id=call_id,
                turns=len(turn_ids),
                linked=linked,
                deferred=deferred,
                **action_counts,
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
        for anneal_round in range(1, 2):
            candidate = candidate or self.store.claim_episode_annealing_candidate(
                self.config.recent_turns, self.config.recent_raw_tokens
            )
            if candidate is None:
                return False
            episode = candidate["episode"]
            episode_id = str(episode["id"])
            user_prompt = render_episode_annealing_request(
                episode, candidate["messages"]
            )
            request = [{"role": "user", "content": user_prompt}]
            try:
                call_id = new_trace_id()
                with log_context(
                    stage="episode_anneal",
                    turn_id=turn_id,
                    call_id=call_id,
                    round=anneal_round,
                    episode_id=episode_id,
                ):
                    completion = self.provider.complete(
                        EPISODE_SUMMARY_SYSTEM_PROMPT, request, []
                    )
                    response = (
                        await asyncio.wait_for(completion, timeout=max_seconds)
                        if max_seconds is not None
                        else await completion
                    )
                summary = self._context_plan_response_text(response.content)
                summary = re.sub(
                    r"<think>.*?</think>", "", summary, flags=re.DOTALL
                ).strip()
                if not summary:
                    raise RuntimeError("episode summary provider returned no text")
                result = self._episode_summary_result(summary)
                working_summary = self.store.finish_episode_annealing(
                    episode_id,
                    int(candidate["through_ordinal"]),
                    result["claims"],  # type: ignore[arg-type]
                    narrative_summary=str(result["narrative_summary"]),
                    emotional_context=result["emotional_context"],  # type: ignore[arg-type]
                    outcomes=result["outcomes"],  # type: ignore[arg-type]
                )
                metrics = response.usage or {}
                self.store.record_turn_usage(
                    turn_id,
                    int(
                        metrics.get(
                            "input",
                            estimate_tokens(
                                EPISODE_SUMMARY_SYSTEM_PROMPT
                                + user_prompt
                            ),
                        )
                    ),
                    int(metrics.get("output", estimate_tokens(summary))),
                )
                log_event(
                    logger,
                    logging.DEBUG,
                    "episode_anneal_complete",
                    stage="episode_anneal",
                    turn_id=turn_id,
                    call_id=call_id,
                    round=anneal_round,
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
        return False
