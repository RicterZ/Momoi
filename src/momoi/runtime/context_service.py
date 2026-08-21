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
    render_planner_recent_turn_focus,
    render_planner_recent_turns,
)
from .context_candidates import (
    DEFAULT_EPISODE_CANDIDATE_POLICY,
    EpisodeCandidatePolicy,
    collect_episode_candidates,
    render_candidate_context,
)
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
    sections as _sections,
    plan_log_episodes as _plan_log_episodes,
    plan_log_units as _plan_log_units,
    tool_error_block as _tool_error_block,
)

logger = logging.getLogger("momoi.runtime.turns")

PLANNER_INTERNAL_TOOLS = [
    {
        "id": "memory_search",
        "description": "Search confirmed durable owner memory.",
    },
    {
        "id": "memory_remember",
        "description": "Stage owner-confirmed memory for commit by the Owner Turn.",
    },
    {
        "id": "memory_forget",
        "description": "Stage removal of owner-confirmed memory.",
    },
    {
        "id": "conversation_search",
        "description": "Search archived conversation Episodes and matched messages.",
    },
    {
        "id": "conversation_read",
        "description": "Read exact archived conversation wording after a search hit.",
    },
]


def _planner_state_lines(items: list[dict[str, object]], *, reminder: bool = False) -> str:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if reminder:
            fields = [f"id={item['id']}"]
            if item.get("text"):
                fields.append(f"text={str(item['text'])[:160]}")
            if item.get("fire_timestamp"):
                fields.append(f"at={item['fire_timestamp']}")
        else:
            fields = [
                f"id={item['id']}",
                f"status={item.get('status') or 'unknown'}",
                f"title={str(item.get('title') or '')[:100]}",
            ]
            for key, label, limit in (
                ("next_action", "next", 120),
                ("waiting_for", "waiting", 100),
                ("latest_result", "last", 160),
            ):
                if item.get(key) not in (None, "", [], {}):
                    fields.append(f"{label}={str(item[key])[:limit]}")
        lines.append("- " + " ".join(fields))
    return "\n".join(lines)


def _planner_mcp_lines(items: list[dict[str, object]]) -> str:
    return "\n".join(
        f"- id={item.get('id')} description={str(item.get('description') or '')[:240]}"
        for item in items
        if isinstance(item, dict) and item.get("id")
    )


def _planner_internal_tool_lines(items: list[dict[str, str]]) -> str:
    return "\n".join(
        f"- id={item['id']} description={item['description']}"
        for item in items
    )


def _planner_owner_lines(items: list[dict[str, object]]) -> str:
    blocks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        blocks.append(
            f"[event id={item.get('event_id')} channel={item.get('channel')} "
            f"at={item.get('timestamp')}]\n{item.get('text') or ''}"
        )
    return "\n\n".join(blocks)


def _planner_interrupted_reply_lines(value: str) -> str:
    if not value:
        return ""
    try:
        item = json.loads(value)
    except (TypeError, ValueError):
        return str(value)
    if not isinstance(item, dict):
        return str(value)
    lines = [f"state: {item.get('state') or 'unknown'}"]
    for key, label in (
        ("expected_information", "expected"),
        ("reason", "reason"),
        ("source_turn", "source turn"),
        ("waiting_since", "waiting since"),
        ("interrupted_at", "interrupted at"),
        ("deadline", "deadline"),
        ("delay_minutes", "delay minutes"),
        ("elapsed_minutes", "elapsed minutes"),
    ):
        if item.get(key) not in (None, "", [], {}):
            lines.append(f"{label}: {item[key]}")
    messages = item.get("source_messages")
    if isinstance(messages, list) and messages:
        lines.append("source messages:")
        for message in messages:
            if not isinstance(message, dict):
                continue
            lines.append(
                f"- {message.get('role') or 'message'} at={message.get('timestamp') or '?'} "
                f"delivery={message.get('delivery_state') or 'unknown'}"
            )
            if message.get("content"):
                lines.append(f"  {message['content']}")
    return "\n".join(lines)


def _planner_value(value: str) -> str:
    return value if value.strip() else "(none)"


def _heartbeat_topic_lines(items: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fields: list[str] = []
        for key, limit in (("title", 120), ("updated_timestamp", 32)):
            value = item.get(key)
            if value not in (None, "", [], {}):
                fields.append(f"{key.removesuffix('_timestamp')}={str(value)[:limit]}")
        summary = str(item.get("summary") or "").strip()
        if summary:
            fields.append(f"summary={summary[:240]}")
        for key in ("topics", "entities", "open_loops"):
            values = item.get(key) or []
            if values:
                fields.append(f"{key}=" + ",".join(str(value) for value in values[:8]))
        if fields:
            lines.append("- " + " ".join(fields))
    return "\n".join(lines)


def _heartbeat_activity_lines(items: list[dict[str, str]]) -> str:
    rendered = "\n".join(
        f"- at={item.get('at') or '?'} activity={str(item.get('text') or '').strip()}"
        for item in items
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )
    return rendered or "(none)"


def _heartbeat_self_state_lines(value: str) -> str:
    try:
        state = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(state, dict):
        return str(value)
    lines: list[str] = []
    mood = state.get("mood")
    if isinstance(mood, dict):
        fields = [
            f"state={mood.get('state') or 'unknown'}",
            f"intensity={mood.get('intensity') or 0}",
        ]
        for key in ("cause", "age_minutes", "updated_at"):
            if mood.get(key) not in (None, "", [], {}):
                fields.append(f"{key}={mood[key]}")
        lines.append("mood: " + " ".join(fields))
    activity = state.get("activity")
    if isinstance(activity, dict):
        fields = []
        for key in ("text", "result", "since"):
            value = str(activity.get(key) or "none").replace("\n", " ")
            fields.append(f"{key}={value}")
        lines.append("activity: " + " ".join(fields))
    if state.get("last_heartbeat_at"):
        lines.append(f"last heartbeat: {state['last_heartbeat_at']}")
    return "\n".join(lines) or "(none)"


def _heartbeat_conversation_state_lines(state: dict[str, object]) -> str:
    fields = [
        f"owner event revision: {state.get('owner_event_revision') or 0}",
        f"owner busy: {bool(state.get('owner_busy') or state.get('owner_turn_or_delivery_active'))}",
    ]
    for key in ("blocked_by", "owner_contact_allowed_now", "owner_contact_eligible_at"):
        if state.get(key) not in (None, "", [], {}):
            fields.append(f"{key}: {state[key]}")
    return "\n".join(fields)


def _heartbeat_plan_lines(plan: dict[str, object]) -> str:
    activity = plan.get("activity") if isinstance(plan, dict) else {}
    handoff = plan.get("heartbeat_handoff") if isinstance(plan, dict) else {}
    context = handoff.get("context") if isinstance(handoff, dict) else {}
    mcp = handoff.get("mcp") if isinstance(handoff, dict) else {}
    execution = handoff.get("execution") if isinstance(handoff, dict) else {}
    lines = [
        f"activity: {str(activity.get('intent') or '').strip()}",
        f"reason: {str(activity.get('reason') or '').strip()}",
        f"context status: {context.get('status') or 'sufficient'}",
        f"context reason: {context.get('reason') or ''}",
    ]
    for query in activity.get("recall_queries") or []:
        lines.append(f"recall query: {str(query).replace(chr(10), ' ')}")
    for need in context.get("needs") or []:
        if isinstance(need, dict):
            fields = [
                f"{key}={str(need.get(key) or '').replace(chr(10), ' ')}"
                for key in ("tool", "query", "evidence")
                if need.get(key) not in (None, "", [], {})
            ]
            lines.append("context need: " + " ".join(fields))
    servers = mcp.get("servers") if isinstance(mcp, dict) else []
    lines.append("mcp servers: " + (", ".join(str(item) for item in servers) if servers else "none"))
    lines.append(f"mcp reason: {mcp.get('reason') if isinstance(mcp, dict) else ''}")
    lines.append(f"execution mode: {execution.get('mode') if isinstance(execution, dict) else 'work'}")
    for index, step in enumerate(execution.get("outline") or [], start=1):
        lines.append(f"step {index}: {step}")
    if isinstance(execution, dict) and execution.get("reason"):
        lines.append(f"execution reason: {execution['reason']}")
    for item in plan.get("uncertainty") or []:
        lines.append(f"uncertainty: {item}")
    return "\n".join(lines)


def _pending_owner_reply_lines(value: dict[str, object]) -> str:
    """Render reply-wait state without carrying a JSON metadata envelope."""
    labels = (
        ("source turn", ("source_turn", "pending_reply_turn_id")),
        ("expected information", ("expected_information", "pending_reply_expectation")),
        ("reason", ("reason", "pending_reply_last_reason")),
        ("waiting since", ("waiting_since", "pending_reply_since")),
        ("waiting_minutes", ("waiting_minutes",)),
        ("delay_minutes", ("delay_minutes", "pending_reply_delay_minutes")),
        ("deadline", ("deadline",)),
        ("channel", ("channel", "pending_reply_channel")),
        ("next check", ("next_check_at", "pending_reply_next_check_at")),
    )
    return "\n".join(
        f"{label}: {next((value.get(key) for key in keys if value.get(key) not in (None, '')), 'none')}"
        for label, keys in labels
    )


def _reply_wait_message_lines(
    value: dict[str, object], *, owner_visible: bool
) -> str:
    """Render the source exchange as plain text without duplicating metadata."""
    messages = value.get("source_messages")
    if not isinstance(messages, list):
        return ""
    blocks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        is_sent = role == "assistant"
        if is_sent != owner_visible:
            continue
        label = "MOMOI" if is_sent else role.upper()
        delivery = str(message.get("delivery_state") or "")
        suffix = f" delivery={delivery}" if is_sent and delivery else ""
        blocks.append(
            f"[{label} at={message.get('timestamp') or '?'}{suffix}]\n"
            f"{str(message.get('content') or '').strip()}"
        )
    return "\n\n".join(blocks)


def render_heartbeat_planner_request(
    *,
    internal_tools: list[dict[str, str]],
    mcp_servers: list[dict[str, object]],
    workspace_guidance: str,
    long_term_memories: str,
    recent_memories: str,
    active_goals: str,
    pending_reminders: str,
    recent_topics: list[dict[str, object]],
    recent_turns: dict[str, object],
    recent_turn_base_count: int,
    active_recent_turn_ids: list[str],
    recent_heartbeat_activities: list[dict[str, str]],
    previous_activity: dict[str, object],
    current_self_state: str,
    conversation_state: dict[str, object],
    current_time: str,
) -> str:
    previous_lines = "\n".join(
        f"{key}: {str(previous_activity.get(key) or '(none)').strip()}"
        for key in ("activity", "result")
    )
    turns = recent_turns.get("turns")
    turn_items = turns if isinstance(turns, list) else []
    base_count = max(0, min(int(recent_turn_base_count), len(turn_items)))
    return _sections(
        (
            "available_internal_tools",
            _planner_value(_planner_internal_tool_lines(internal_tools)),
        ),
        ("available_mcp_servers", _planner_value(_planner_mcp_lines(mcp_servers))),
        ("workspace_heartbeat_guidance", _planner_value(workspace_guidance)),
        ("long_term_memories", _planner_value(long_term_memories)),
        ("recent_memories", _planner_value(recent_memories)),
        ("active_goals", _planner_value(active_goals)),
        ("pending_reminders", _planner_value(pending_reminders)),
        (
            "recent_turn_base",
            _planner_value(
                render_planner_recent_turns(
                    {"version": 1, "turns": turn_items[:base_count]}
                )
            ),
        ),
        (
            "recent_turn_append",
            _planner_value(
                render_planner_recent_turns(
                    {"version": 1, "turns": turn_items[base_count:]},
                    start_index=base_count + 1,
                )
            ),
        ),
        (
            "recent_turn_focus",
            _planner_value(
                render_planner_recent_turn_focus(
                    recent_turns,
                    active_recent_turn_ids,
                )
            ),
        ),
        ("recent_topics", _planner_value(_heartbeat_topic_lines(recent_topics))),
        ("recent_heartbeat_activities", _planner_value(_heartbeat_activity_lines(recent_heartbeat_activities))),
        ("previous_activity", _planner_value(previous_lines)),
        ("current_self_state", _planner_value(_heartbeat_self_state_lines(current_self_state))),
        ("conversation_state", _planner_value(_heartbeat_conversation_state_lines(conversation_state))),
        ("current_time", _planner_value(current_time)),
    )


def render_context_planner_request(
    *,
    internal_tools: list[dict[str, str]],
    mcp_servers: list[dict[str, object]],
    long_term_memories: str,
    recent_memories: str,
    recent_turns: dict[str, object],
    recent_turn_base_count: int,
    active_recent_turn_ids: list[str],
    candidate_goals: list[dict[str, object]],
    candidate_reminders: list[dict[str, object]],
    candidate_episodes: list[dict[str, object]],
    interrupted_reply_expectation: str,
    owner_messages: list[dict[str, object]],
) -> str:
    """Serialize the exact human-readable user prompt sent to Context Planner."""
    turns = recent_turns.get("turns")
    turn_items = turns if isinstance(turns, list) else []
    base_count = max(0, min(int(recent_turn_base_count), len(turn_items)))
    return _sections(
        (
            "available_internal_tools",
            _planner_value(_planner_internal_tool_lines(internal_tools)),
        ),
        (
            "available_mcp_servers",
            _planner_value(_planner_mcp_lines(mcp_servers)),
        ),
        ("long_term_memories", _planner_value(long_term_memories)),
        ("recent_memories", _planner_value(recent_memories)),
        (
            "candidate_goals",
            _planner_value(_planner_state_lines(candidate_goals)),
        ),
        (
            "candidate_reminders",
            _planner_value(_planner_state_lines(candidate_reminders, reminder=True)),
        ),
        (
            "interrupted_reply_expectation",
            _planner_value(
                _planner_interrupted_reply_lines(interrupted_reply_expectation)
            ),
        ),
        (
            "recent_turn_base",
            _planner_value(
                render_planner_recent_turns(
                    {"version": 1, "turns": turn_items[:base_count]}
                )
            ),
        ),
        (
            "recent_turn_append",
            _planner_value(
                render_planner_recent_turns(
                    {"version": 1, "turns": turn_items[base_count:]},
                    start_index=base_count + 1,
                )
            ),
        ),
        (
            "recent_turn_focus",
            _planner_value(
                render_planner_recent_turn_focus(
                    recent_turns,
                    active_recent_turn_ids,
                )
            ),
        ),
        (
            "candidate_episodes",
            _planner_value(render_candidate_context(candidate_episodes)),
        ),
        (
            "owner_messages",
            _planner_value(_planner_owner_lines(owner_messages)),
        ),
    )


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
        planner_recent_turns, active_recent_turn_ids, recent_turn_base_count = (
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
        owner_messages = [
            {
                "event_id": event.event_id,
                "channel": event.channel,
                "timestamp": context_timestamp(event.occurred_at),
                "text": event.text,
            }
            for event in events
        ]
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
            for goal in self.store.list_goals()[
                : self.config.policies.context.max_visible_goals
            ]
        ]
        candidate_reminders = [
            {
                name: reminder.get(name)
                for name in ("id", "text", "fire_timestamp", "schedule")
            }
            for reminder in self.store.list_reminders(
                self.config.policies.context.max_visible_reminders
            )
        ]
        interrupted_reply = self.store.cooled_reply_expectation_context()
        # Prefix-cache order: fixed memory and agenda fields precede append-only
        # conversation evidence; query-specific episodes and the owner message
        # remain at the tail.
        request: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": render_context_planner_request(
                    internal_tools=PLANNER_INTERNAL_TOOLS,
                    mcp_servers=mcp_server_catalog,
                    long_term_memories=self.store.always_memory_context(),
                    recent_memories=self.store.recent_memory_context(
                        max(100, self.config.memory_tokens // 8)
                    ),
                    recent_turns=planner_recent_turns,
                    recent_turn_base_count=recent_turn_base_count,
                    active_recent_turn_ids=active_recent_turn_ids,
                    candidate_goals=candidate_goals,
                    candidate_reminders=candidate_reminders,
                    candidate_episodes=candidates,
                    interrupted_reply_expectation=interrupted_reply,
                    owner_messages=owner_messages,
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
        if (
            not isinstance(retrieval, dict)
            or retrieval.get("version") != 3
            or not isinstance(retrieval.get("recall_memories"), list)
            or not isinstance(retrieval.get("reflection_memories"), list)
            or not isinstance(retrieval.get("recalled_turns"), list)
        ):
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
        goals: str,
        reminders: str,
        long_term_memories: str,
        recent_memories: str,
    ) -> dict[str, object]:
        mcp_server_catalog = self._heartbeat_mcp_server_catalog()
        available_mcp_servers = {
            str(server["id"]) for server in mcp_server_catalog
        }
        planner_recent_turns, active_recent_turn_ids, recent_turn_base_count = (
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
            )
        )
        request = [
            {
                "role": "user",
                "content": render_heartbeat_planner_request(
                    internal_tools=PLANNER_INTERNAL_TOOLS,
                    mcp_servers=mcp_server_catalog,
                    workspace_guidance=self._workspace_heartbeat_guidance(),
                    long_term_memories=long_term_memories,
                    recent_memories=recent_memories,
                    active_goals=goals,
                    pending_reminders=reminders,
                    recent_turns=planner_recent_turns,
                    recent_turn_base_count=recent_turn_base_count,
                    active_recent_turn_ids=active_recent_turn_ids,
                    recent_topics=recent_topics,
                    recent_heartbeat_activities=self.store.recent_heartbeat_activities(),
                    previous_activity={
                        "activity": state.get("activity"),
                        "result": state.get("activity_result"),
                    },
                    current_self_state=self_context,
                    conversation_state=conversation,
                    current_time=datetime.now().astimezone().isoformat(timespec="seconds"),
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
