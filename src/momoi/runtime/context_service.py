import json
import logging
import re
import time
import uuid
from datetime import datetime

from ..agenda_tools import AGENDA_TOOL_SPECS
from ..builtin_tools import BUILTIN_TOOL_SPECS
from ..logging_context import log_context, log_event, new_trace_id
from ..memory_tools import MEMORY_TOOL_SPECS
from ..models import IncomingMessage
from ..storage import MemoryRecallQuery
from .context_assembler import (
    assemble_main_context,
    assemble_planner_recent_turns,
    assemble_recent_external_events,
    build_plan_retrieval,
    recall_query_semantic,
    select_plan_recall_queries,
    render_planner_recent_turn_focus,
    render_planner_recent_turns,
)
from .heartbeat_planner import (
    HEARTBEAT_PLAN_TOOL_NAME,
    HEARTBEAT_PLAN_TOOL_SPEC,
    HeartbeatPlanError,
    degraded_heartbeat_plan,
    parse_heartbeat_plan,
)
from .turn_support import (
    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
    sections as _sections,
    tool_error_block as _tool_error_block,
)

logger = logging.getLogger("momoi.runtime.turns")
# Episode reference syntax the runtime already stores.
_NEW_EPISODE_SLUG = re.compile(r"new:[a-z0-9][a-z0-9_-]{0,39}")

PLANNER_INTERNAL_TOOLS = [
    {
        "id": str(spec["name"]),
        "description": str(spec.get("description") or ""),
    }
    for spec in (*MEMORY_TOOL_SPECS, *AGENDA_TOOL_SPECS, *BUILTIN_TOOL_SPECS)
]


def _planner_state_lines(items: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
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
        f"- id={item['id']} description="
        f"{' '.join(item['description'].split())[:240]}"
        for item in items
    )


def _planner_owner_lines(items: list[dict[str, object]]) -> str:
    blocks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        blocks.append(
            f"[event id={item.get('event_id')} at={item.get('timestamp')}]\n"
            f"{item.get('text') or ''}"
        )
    return "\n\n".join(blocks)


def _planner_episode_lines(items: list[dict[str, object]]) -> str:
    blocks: list[str] = []
    for episode in items:
        fields = [
            f"id={episode['id']}",
            f"status={episode['status']}",
            f"title={str(episode['title'])[:120]}",
        ]
        summary = str(
            episode.get("narrative_summary")
            or episode.get("working_summary")
            or ""
        ).strip()
        if summary:
            fields.append(f"summary={summary[:240]}")
        topics = episode.get("topics") or []
        if topics:
            fields.append("topics=" + ",".join(str(item) for item in topics[:8]))
        loops = episode.get("open_loops") or []
        if loops:
            fields.append(
                "open_loops=" + ",".join(str(item) for item in loops[:4])
            )
        blocks.append("- " + " ".join(fields))
    return "\n".join(blocks)


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
        f"recall mode: {activity.get('recall_mode') or 'skip'}",
        f"context status: {context.get('status') or 'sufficient'}",
        f"context reason: {context.get('reason') or ''}",
    ]
    for query in activity.get("recall_queries") or []:
        if isinstance(query, dict):
            semantic = recall_query_semantic(query)
            keywords = ", ".join(str(item) for item in query.get("keywords") or [])
            lines.append(f"recall semantic: {semantic}")
            lines.append(f"recall keywords: {keywords or 'none'}")
        else:
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


def _planner_recall_context_lines(
    values: list[dict[str, object]],
) -> str:
    lines: list[str] = []
    for value in values:
        turn_id = str(value.get("turn_id") or "")
        queries = [str(item) for item in value.get("queries") or []]
        if not turn_id or not queries:
            continue
        lines.append(f"turn={turn_id} queries=" + " ; ".join(queries))
    return "\n".join(lines)


def render_heartbeat_planner_request(
    *,
    internal_tools: list[dict[str, str]],
    mcp_servers: list[dict[str, object]],
    workspace_guidance: str,
    long_term_memories: str,
    recent_memories: str,
    active_goals: str,
    recent_topics: list[dict[str, object]],
    recent_turns: dict[str, object],
    recent_turn_base_count: int,
    active_recent_turn_ids: list[str],
    recent_heartbeat_activities: list[dict[str, str]],
    previous_activity: dict[str, object],
    current_self_state: str,
    conversation_state: dict[str, object],
    current_time: str,
    recent_external_events: str = "",
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
        ("recent_external_events", _planner_value(recent_external_events)),
        ("recent_topics", _planner_value(_heartbeat_topic_lines(recent_topics))),
        ("recent_heartbeat_activities", _planner_value(_heartbeat_activity_lines(recent_heartbeat_activities))),
        ("previous_activity", _planner_value(previous_lines)),
        ("current_self_state", _planner_value(_heartbeat_self_state_lines(current_self_state))),
        ("conversation_state", _planner_value(_heartbeat_conversation_state_lines(conversation_state))),
        ("current_time", _planner_value(current_time)),
    )




class ContextService:
    def _plan_from_submission(
        self,
        events: list[IncomingMessage],
        arguments: dict[str, object],
        *,
        turn_id: str,
        revision: int,
    ) -> dict[str, object]:
        """Shape an Owner context submission like a stored plan.

        The retrieval path already knows how to turn recall dispositions and
        Episode actions into evidence; only its source moves, from a separate
        planning model to the Owner's own first action.
        """

        event_ids = [event.event_id for event in events]
        units: list[dict[str, object]] = []
        episodes: list[dict[str, object]] = []
        raw_units = arguments.get("units")
        if not isinstance(raw_units, list) or not raw_units:
            raise ValueError("recall requires at least one intent unit")
        candidate_ids = {
            str(item["id"])
            for item in self.store.list_recent_episode_directory(
                8, exclude_runtime_archives=True
            )
            if item.get("id")
        }
        for index, raw in enumerate(raw_units if isinstance(raw_units, list) else [], 1):
            if not isinstance(raw, dict):
                raise ValueError("each recall unit must be an object")
            unit_id = f"u{index}"
            mode = str(raw.get("recall_mode") or "search")
            queries = [
                {
                    "semantic": " ".join(str(query.get("semantic") or "").split())[:240],
                    "keywords": [
                        " ".join(str(keyword).split())[:60]
                        for keyword in (query.get("keywords") or [])
                        if " ".join(str(keyword).split())
                    ],
                }
                for query in (raw.get("recall_queries") or [])
                if isinstance(query, dict) and str(query.get("semantic") or "").strip()
            ][:3]
            from_turn_id = str(raw.get("recall_from_turn_id") or "")
            if mode not in {"search", "reuse"}:
                raise ValueError("recall_mode must be search or reuse")
            if mode == "search":
                if not queries:
                    raise ValueError("search recall requires at least one query")
                from_turn_id = ""
            elif not from_turn_id or not self.store.recall_reuse_candidates(
                [from_turn_id]
            ):
                raise ValueError("reuse requires a displayed recalled Turn")
            units.append(
                {
                    "id": unit_id,
                    "event_ids": event_ids,
                    "intent": " ".join(str(raw.get("intent") or "").split())[:160],
                    "recall_mode": mode,
                    "recall_queries": queries if mode == "search" else [],
                    "recall_from_turn_id": from_turn_id if mode == "reuse" else "",
                    "recall": {
                        "mode": mode,
                        "from_turn_id": from_turn_id if mode == "reuse" else "",
                        "queries": queries if mode == "search" else [],
                    },
                }
            )
            episode = raw.get("episode")
            action = (
                str(episode.get("action") or "none")
                if isinstance(episode, dict)
                else "none"
            )
            if action not in {"none", "continue", "new"}:
                raise ValueError("episode action must be none, continue, or new")
            if action == "none":
                continue
            binding: dict[str, object] = {"action": action, "unit_ids": [unit_id]}
            reference = str(episode.get("ref") or "") if isinstance(episode, dict) else ""
            title = str(episode.get("title") or "") if isinstance(episode, dict) else ""
            if action == "continue" and reference in candidate_ids:
                binding["episode_id"] = reference
                binding["episode_ref"] = reference
            elif action == "new" and title and _NEW_EPISODE_SLUG.fullmatch(reference):
                binding["episode_id"] = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"momoi:episode:{turn_id}:{revision}:{reference}",
                ).hex
                binding["title"] = title[:80]
                binding["episode_ref"] = reference
            else:
                raise ValueError("episode reference does not match its action")
            episodes.append(binding)
        return {
            "version": 7,
            "intent_units": units,
            "episode_actions": episodes,
            "episode_links": [],
            "uncertainty": [],
        }

    def owner_context_baseline(
        self, events: list[IncomingMessage]
    ) -> dict[str, str]:
        """Assemble the context that holds before any recall decision is made.

        The fixed memory baseline, Goals and folded external events do not
        depend on what this input turns out to need, so they are available
        before the Owner decides anything. Query-driven evidence arrives later,
        as the result of that decision.
        """

        retrieval = build_plan_retrieval(
            self.store,
            {"version": 7, "intent_units": [], "episode_actions": []},
            self.config,
        )
        return assemble_main_context(
            self.store,
            retrieval,
            self.config.summary_tokens,
            recent_before_timestamp=min(event.received_at for event in events),
        )

    def owner_context_candidates(self, turn_ids: list[str]) -> dict[str, str]:
        """Give the Owner the two catalogs its context decision depends on.

        Continuing an Episode requires seeing which ones are open, and reusing a
        previous recall requires seeing what that recall actually searched for.
        Both were previously visible only to the planning model, which is why
        that model appeared to know something the Owner could not.
        """

        return {
            "candidate_episodes": _planner_episode_lines(
                self.store.list_recent_episode_directory(
                    8, exclude_runtime_archives=True
                )
            ),
            "recent_recall_context": _planner_recall_context_lines(
                self.store.recall_reuse_candidates(turn_ids)
            ),
        }

    async def submit_owner_context(
        self,
        events: list[IncomingMessage],
        turn_id: str,
        arguments: dict[str, object],
    ) -> dict[str, str]:
        """Persist the Owner's context decision and return the evidence it asked for."""

        record = self.store.context_plan(turn_id)
        revision = int(record["revision"]) + 1 if record is not None else 1
        plan = self._plan_from_submission(
            events,
            arguments,
            turn_id=turn_id,
            revision=revision,
        )
        saved = self.store.save_context_plan(
            turn_id, revision, [event.event_id for event in events], plan
        )
        selected, _reused, _emitted, _skipped = select_plan_recall_queries(plan)
        dense_evidence = await self.semantic_recall.prepare(
            [
                MemoryRecallQuery(
                    expression=str(item["expression"]),
                    unit_ids=tuple(str(value) for value in item["unit_ids"]),
                    priority=int(item["priority"]),
                    semantic_expression=str(item["semantic_expression"]),
                )
                for item in selected
            ],
            output_limit=max(self.config.memory_results, self.config.summary_results),
        )
        retrieval = build_plan_retrieval(
            self.store, plan, self.config, dense_evidence=dense_evidence
        )
        stored = self.store.save_context_retrieval(
            turn_id, int(saved["revision"]), retrieval, state="recalled"
        )
        return assemble_main_context(
            self.store,
            stored["retrieval"],
            self.config.summary_tokens,
            recent_before_timestamp=min(event.received_at for event in events),
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
                self.config.recent_turns,
                self.config.recent_turns,
                self.config.recent_turns,
                min(
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
                    recent_turns=planner_recent_turns,
                    recent_turn_base_count=recent_turn_base_count,
                    active_recent_turn_ids=active_recent_turn_ids,
                    recent_external_events=assemble_recent_external_events(
                        self.store
                    ),
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
                    raise HeartbeatPlanError("heartbeat_plan_tool_required")
                plan = parse_heartbeat_plan(
                    response.tool_calls[0].arguments,
                    available_mcp_servers,
                )
            except HeartbeatPlanError as error:
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
