import json
import logging
import time
from datetime import datetime
from typing import Any

from ..context_time import context_timestamp
from ..logging_context import TRACE, compact_log_value, log_context, log_event, new_trace_id, safe_preview
from ..models import IncomingMessage
from ..storage import estimate_tokens
from .context_assembler import (
    assemble_main_context,
    assemble_planner_recent_turns,
    build_plan_retrieval,
)
from .context_candidates import DEFAULT_EPISODE_CANDIDATE_POLICY, EpisodeCandidatePolicy, collect_episode_candidates, full_candidate_context
from .context_planner import (
    CONTEXT_PLAN_TOOL_NAME,
    CONTEXT_PLAN_TOOL_SPEC,
    HEARTBEAT_PLAN_TOOL_NAME,
    HEARTBEAT_PLAN_TOOL_SPEC,
    ContextPlanError,
    degraded_context_plan,
    degraded_heartbeat_plan,
    parse_context_plan,
    parse_heartbeat_plan,
)
from .turn_support import (
    CONTEXT_PLANNER_SYSTEM_PROMPT,
    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
    OwnerMessagesChanged,
    plan_log_episodes as _plan_log_episodes,
    plan_log_units as _plan_log_units,
    tool_error_block as _tool_error_block,
)

logger = logging.getLogger("momoi.runtime.turns")

class ContextService:
    @staticmethod
    def _stored_context_plan(record: dict[str, object]) -> dict[str, object]:
        plan = record.get("plan")
        if not isinstance(plan, dict):
            raise RuntimeError("stored context plan is not an object")
        return plan

    async def _plan_owner_context(
        self,
        events: list[IncomingMessage],
        turn_id: str,
        candidate_policy: EpisodeCandidatePolicy = DEFAULT_EPISODE_CANDIDATE_POLICY,
    ) -> dict[str, object]:
        event_ids = [event.event_id for event in events]
        active = self.store.context_plan(turn_id)
        if active is not None and active["source_event_ids"] == event_ids:
            return self._stored_context_plan(active)

        revision = self.store.next_context_plan_revision(turn_id)
        owner_query = "\n".join(event.text for event in events)
        mcp_server_catalog = self._mcp_server_catalog()
        available_mcp_servers = {
            str(group["id"]) for group in mcp_server_catalog
        }
        planner_recent_turns, active_recent_turn_ids = (
            assemble_planner_recent_turns(
                self.store,
                self.config.planner_recent_base_turns or self.config.recent_turns,
                self.config.planner_recent_append_turns or self.config.recent_turns,
                self.config.planner_active_recent_turns or self.config.recent_turns,
                self.config.planner_recent_tokens
                or min(
                    88000,
                    max(1000, int(self.config.max_input_tokens * 0.55)),
                ),
                min(event.received_at for event in events),
            )
        )
        candidates = collect_episode_candidates(
            self.store,
            owner_query,
            candidate_policy,
            recent_turn_ids=active_recent_turn_ids,
        )
        log_event(
            logger,
            TRACE,
            "episode_candidates_ranked",
            stage="context_plan",
            turn_id=turn_id,
            revision=revision,
            candidates=[
                {
                    "id": candidate["id"],
                    "title": candidate["title"],
                    "status": candidate["status"],
                    "score": candidate.get("match_score"),
                    "features": candidate.get("match_features"),
                    "signals": candidate.get("match_signals"),
                }
                for candidate in candidates
            ],
        )
        candidate_context = full_candidate_context(candidates)
        owner_messages = [
            {
                "event_id": event.event_id,
                "channel": event.channel,
                "timestamp": context_timestamp(event.occurred_at),
                "text": event.text,
            }
            for event in events
        ]
        goals_by_id: dict[str, dict[str, object]] = {}
        for goal in [
            *self.store.search_goals(
                owner_query, self.config.policies.context.max_visible_goals
            ),
            *self.store.list_goals(),
        ]:
            goals_by_id.setdefault(str(goal["id"]), goal)
            if len(goals_by_id) == self.config.policies.context.max_visible_goals:
                break
        candidate_goals = [
            {
                name: goal.get(name)
                for name in (
                    "id",
                    "status",
                    "title",
                    "next_action",
                    "waiting_for",
                    "latest_result",
                    "next_review_timestamp",
                )
            }
            for goal in goals_by_id.values()
        ]
        reminders_by_id: dict[str, dict[str, object]] = {}
        for reminder in [
            *self.store.search_reminders(
                owner_query, self.config.policies.context.max_visible_reminders
            ),
            *self.store.list_reminders(
                self.config.policies.context.max_visible_reminders
            ),
        ]:
            reminders_by_id.setdefault(str(reminder["id"]), reminder)
            if (
                len(reminders_by_id)
                == self.config.policies.context.max_visible_reminders
            ):
                break
        candidate_reminders = [
            {
                name: reminder.get(name)
                for name in ("id", "text", "fire_timestamp", "schedule")
            }
            for reminder in reminders_by_id.values()
        ]
        interrupted_reply = self.store.cooled_reply_expectation_context()
        request: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "candidate_goals": candidate_goals,
                        "candidate_reminders": candidate_reminders,
                        "available_mcp_servers": mcp_server_catalog,
                        "recent_turns": planner_recent_turns,
                        "active_recent_turn_ids": active_recent_turn_ids,
                        "candidate_episodes": candidate_context,
                        "interrupted_reply_expectation": (
                            json.loads(interrupted_reply)
                            if interrupted_reply
                            else None
                        ),
                        "owner_messages": owner_messages,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ]
        last_error = "invalid_context_plan"
        planner_tools = [CONTEXT_PLAN_TOOL_SPEC]
        for attempt in range(2):
            raw_plan: object = None
            call_started = time.monotonic()
            call_id = new_trace_id()
            with log_context(
                stage="context_plan",
                turn_id=turn_id,
                call_id=call_id,
                round=attempt + 1,
            ):
                self._check_turn_budget(
                    turn_id,
                    CONTEXT_PLANNER_SYSTEM_PROMPT,
                    request,
                    planner_tools,
                )
                response = await self._complete_with_owner_interrupt(
                    CONTEXT_PLANNER_SYSTEM_PROMPT,
                    request,
                    planner_tools,
                    require_tool=True,
                    current_events=events,
                    channel_name=self._channel_for(events[0].channel).name,
                )
            metrics = response.usage or {}
            self.store.record_turn_usage(
                turn_id,
                int(
                    metrics.get(
                        "input",
                        estimate_tokens(
                            json.dumps(
                                {
                                    "system": CONTEXT_PLANNER_SYSTEM_PROMPT,
                                    "messages": request,
                                    "tools": planner_tools,
                                },
                                ensure_ascii=False,
                            )
                        ),
                    )
                ),
                int(
                    metrics.get(
                        "output",
                        estimate_tokens(
                            json.dumps(response.content, ensure_ascii=False)
                        ),
                    )
                ),
            )
            try:
                if (
                    len(response.tool_calls) != 1
                    or response.tool_calls[0].name != CONTEXT_PLAN_TOOL_NAME
                ):
                    raise ContextPlanError("context_plan_tool_required")
                raw_plan = response.tool_calls[0].arguments
                log_event(
                    logger,
                    logging.DEBUG,
                    "context_plan_received",
                    stage="context_plan",
                    turn_id=turn_id,
                    call_id=call_id,
                    round=attempt + 1,
                    revision=revision,
                    tool_call_id=response.tool_calls[0].id,
                    version=raw_plan.get("version"),
                    intent_units=safe_preview(raw_plan.get("intent_units"), 900),
                    episode_actions=safe_preview(
                        raw_plan.get("episode_actions"),
                        900,
                    ),
                    raw_plan=compact_log_value(raw_plan, string_limit=300),
                )
                plan = parse_context_plan(
                    raw_plan,
                    event_ids,
                    candidates,
                    turn_id,
                    revision,
                    available_mcp_servers,
                )
            except ContextPlanError as error:
                last_error = str(error)
                log_event(
                    logger,
                    logging.WARNING,
                    "context_plan_invalid",
                    stage="context_plan",
                    turn_id=turn_id,
                    call_id=call_id,
                    round=attempt + 1,
                    revision=revision,
                    reason=last_error,
                    tool_calls=[
                        {"name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                    raw_plan=raw_plan,
                    duration_ms=int((time.monotonic() - call_started) * 1000),
                )
                if attempt == 0:
                    correction = (
                        "[Trusted protocol correction: the previous context plan "
                        f"failed validation with {last_error}. Call "
                        f"{CONTEXT_PLAN_TOOL_NAME} exactly once with corrected arguments.]"
                    )
                    correction_content = [
                        *[
                            _tool_error_block(call.id, last_error)
                            for call in response.tool_calls
                        ],
                        {"type": "text", "text": correction},
                    ]
                    request.extend(
                        [
                            {"role": "assistant", "content": response.content},
                            {
                                "role": "user",
                                "content": correction_content,
                            },
                        ]
                    )
                    continue
                plan = degraded_context_plan(owner_messages, last_error)
                log_event(
                    logger,
                    logging.WARNING,
                    "context_plan_degraded",
                    stage="context_plan",
                    turn_id=turn_id,
                    call_id=call_id,
                    round=attempt + 1,
                    revision=revision,
                    reason=last_error,
                    duration_ms=int((time.monotonic() - call_started) * 1000),
                )
                saved = self.store.save_context_plan(
                    turn_id, revision, event_ids, plan, state="degraded"
                )
                return self._stored_context_plan(saved)
            saved = self.store.save_context_plan(turn_id, revision, event_ids, plan)
            units = plan.get("intent_units")
            bindings = plan.get("episode_actions")
            log_event(
                logger,
                logging.INFO,
                "context_plan_complete",
                stage="context_plan",
                turn_id=turn_id,
                call_id=call_id,
                round=attempt + 1,
                revision=revision,
                units=len(units) if isinstance(units, list) else 0,
                episodes=len(bindings) if isinstance(bindings, list) else 0,
                plan_units=_plan_log_units(plan),
                episode_actions=_plan_log_episodes(plan),
                uncertainty=plan.get("uncertainty", []),
                owner_handoff=plan.get("owner_handoff"),
                duration_ms=int((time.monotonic() - call_started) * 1000),
            )
            return self._stored_context_plan(saved)
        raise RuntimeError("context planner retry loop ended unexpectedly")

    async def _prepare_owner_context(
        self, events: list[IncomingMessage], turn_id: str
    ) -> tuple[dict[str, object], dict[str, str]]:
        while True:
            try:
                plan = await self._plan_owner_context(events, turn_id)
                break
            except OwnerMessagesChanged:
                await self._settle_owner_updates(
                    events, self._channel_for(events[0].channel).name
                )
        record = self.store.context_plan(turn_id)
        if record is None:
            raise RuntimeError("active context plan was not saved")
        retrieval = record.get("retrieval")
        if not isinstance(retrieval, dict) or retrieval.get("version") != 2:
            retrieval = build_plan_retrieval(self.store, plan, self.config)
            record = self.store.save_context_retrieval(
                turn_id,
                int(record["revision"]),
                retrieval,
                state=("degraded" if record["state"] == "degraded" else "recalled"),
            )
            retrieval = record["retrieval"]
        if not isinstance(retrieval, dict):
            raise RuntimeError("stored context retrieval is not an object")
        return plan, assemble_main_context(
            self.store,
            retrieval,
            self.config.summary_tokens,
            self.config.recent_raw_tokens,
            self.config.recent_turns,
            min(event.received_at for event in events),
        )

    async def _plan_heartbeat_context(
        self,
        turn_id: str,
        *,
        state: dict[str, object],
        self_context: str,
        conversation: dict[str, object],
        recent_topics: list[dict[str, object]],
        recent_conversation: str,
        goals: str,
        reminders: str,
    ) -> dict[str, object]:
        mcp_server_catalog = self._heartbeat_mcp_server_catalog()
        available_mcp_servers = {
            str(server["id"]) for server in mcp_server_catalog
        }
        request = [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "available_mcp_servers": mcp_server_catalog,
                        "current_time": datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                        "current_self_state": self_context,
                        "previous_activity": {
                            "activity": state.get("activity"),
                            "result": state.get("activity_result"),
                        },
                        "conversation_state": conversation,
                        "recent_topics": recent_topics,
                        "recent_conversation": recent_conversation,
                        "active_goals": goals,
                        "pending_reminders": reminders,
                        "workspace_heartbeat_guidance": (
                            self._workspace_heartbeat_guidance()
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ]
        last_error = "invalid_heartbeat_plan"
        for attempt in range(2):
            call_id = new_trace_id()
            started = time.monotonic()
            with log_context(
                stage="heartbeat_plan",
                turn_id=turn_id,
                call_id=call_id,
                round=attempt + 1,
            ):
                self._check_turn_budget(
                    turn_id,
                    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
                    request,
                    [HEARTBEAT_PLAN_TOOL_SPEC],
                )
                response = await self.provider.complete(
                    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
                    request,
                    [HEARTBEAT_PLAN_TOOL_SPEC],
                    require_tool=True,
                )
            metrics = response.usage or {}
            self.store.record_turn_usage(
                turn_id,
                int(metrics.get("input", 0)),
                int(metrics.get("output", 0)),
            )
            try:
                if (
                    len(response.tool_calls) != 1
                    or response.tool_calls[0].name != HEARTBEAT_PLAN_TOOL_NAME
                ):
                    raise ContextPlanError("heartbeat_plan_tool_required")
                plan = parse_heartbeat_plan(
                    response.tool_calls[0].arguments,
                    available_mcp_servers,
                )
            except ContextPlanError as error:
                last_error = str(error)
                log_event(
                    logger,
                    logging.WARNING,
                    "heartbeat_plan_invalid",
                    stage="heartbeat_plan",
                    turn_id=turn_id,
                    call_id=call_id,
                    round=attempt + 1,
                    reason=last_error,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                if attempt == 0:
                    request.extend(
                        [
                            {"role": "assistant", "content": response.content},
                            {
                                "role": "user",
                                "content": [
                                    *[
                                        _tool_error_block(call.id, last_error)
                                        for call in response.tool_calls
                                    ],
                                    {
                                        "type": "text",
                                        "text": (
                                            "[Trusted protocol correction: call "
                                            f"{HEARTBEAT_PLAN_TOOL_NAME} exactly once "
                                            "with corrected arguments.]"
                                        ),
                                    },
                                ],
                            },
                        ]
                    )
                    continue
                break
            log_event(
                logger,
                logging.INFO,
                "heartbeat_plan_complete",
                stage="heartbeat_plan",
                turn_id=turn_id,
                call_id=call_id,
                round=attempt + 1,
                intent=plan["activity"]["intent"],
                heartbeat_handoff=plan["heartbeat_handoff"],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return plan
        plan = degraded_heartbeat_plan(str(state.get("activity") or ""), last_error)
        log_event(
            logger,
            logging.WARNING,
            "heartbeat_plan_degraded",
            stage="heartbeat_plan",
            turn_id=turn_id,
            reason=last_error,
        )
        return plan
