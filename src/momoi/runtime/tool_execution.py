import copy
import fnmatch
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..agenda_tools import AGENDA_TOOL_SPECS
from ..builtin_tools import BUILTIN_TOOL_SPECS, SELF_DIRECTED_BUILTIN_TOOL_SPECS
from ..channel import Channel, ChannelMessage
from ..contracts import ToolResult
from ..emotions import EMOTION_PREFIX, emotion_slug
from ..logging_context import TRACE, compact_log_value, log_context, log_event, new_trace_id, safe_preview
from ..memory_tools import MEMORY_TOOL_SPECS
from ..models import AgentReply, IncomingMessage, ToolCall, TurnDraft
from ..storage import estimate_tokens
from .progress_announce import (
    announce_field,
    apply_tool_announce,
    decorate_tool_spec,
    initial_announce_error_message,
    should_announce,
    should_deliver_announce,
)
from .parsing import parse_messages, parse_response, response_text
from .protocol import (
    AUTONOMOUS_FINISH_SPEC,
    owner_end_turn_tool_spec,
    send_message_tool_spec,
    tool_enable_spec,
)
from .turn_support import (
    ExternalToolTurnError,
    MAX_CONSECUTIVE_TOOL_FAILURES,
    OwnerMessagesChanged,
    TurnBudgetExceeded,
    tool_error_block as _tool_error_block,
    tool_result_block as _tool_result_block,
    truncate_tool_result_json as _truncate_tool_result_json,
)

logger = logging.getLogger("momoi.runtime.turns")
MAX_TOOL_RESULT_TRUNCATION_ATTEMPTS = 16


class ToolExecutionService:
    @staticmethod
    def _mcp_tool_group(name: str) -> str:
        parts = str(name).split("__", 2)
        return parts[1] if len(parts) == 3 else "other"

    def _mcp_server_groups(self) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in self._announced_tool_specs(
            sorted(
                self.mcp.tool_specs,
                key=lambda item: str(item.get("name") or ""),
            ),
            mcp=True,
        ):
            group = self._mcp_tool_group(str(spec.get("name") or ""))
            groups.setdefault(group, []).append(spec)
        return dict(sorted(groups.items()))

    def _mcp_server_catalog(self) -> list[dict[str, object]]:
        return [
            {
                "id": group,
                "description": self._mcp_group_description(group),
            }
            for group in self._mcp_server_groups()
        ]

    def _mcp_group_description(self, group: str) -> str:
        configs = getattr(self.mcp, "configs", {})
        config = configs.get(group) if isinstance(configs, dict) else None
        description = (
            str(config.get("description") or "").strip()
            if isinstance(config, dict)
            else ""
        )
        return description or f"External MCP capabilities provided by {group}."

    def _owner_internal_tool_surface(self) -> list[dict[str, Any]]:
        return [
            *copy.deepcopy(MEMORY_TOOL_SPECS),
            *self._announced_tool_specs(AGENDA_TOOL_SPECS, mcp=False),
            *self._announced_tool_specs(BUILTIN_TOOL_SPECS, mcp=False),
        ]

    def _owner_enable_tool_groups(self) -> dict[str, list[dict[str, Any]]]:
        return self._mcp_server_groups()

    @staticmethod
    def _append_visible_tool_specs(
        tools: list[dict[str, Any]], specs: list[dict[str, Any]]
    ) -> list[str]:
        existing = {str(spec.get("name") or "") for spec in tools}
        added: list[str] = []
        for spec in specs:
            name = str(spec.get("name") or "")
            if not name or name in existing:
                continue
            tools.insert(max(0, len(tools) - 1), spec)
            existing.add(name)
            added.append(name)
        return added

    @staticmethod
    def _tool_schema_tokens(specs: list[dict[str, Any]]) -> int:
        return estimate_tokens(
            json.dumps(
                specs,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )

    def _log_owner_tool_projection(
        self,
        *,
        visible: list[dict[str, Any]],
        full: list[dict[str, Any]],
    ) -> None:
        visible_tokens = self._tool_schema_tokens(visible)
        full_tokens = self._tool_schema_tokens(full)
        log_event(
            logger,
            TRACE,
            "tool_availability_projected",
            stage="owner",
            visible_tool_count=len(visible),
            full_tool_count=len(full),
            hidden_tool_count=max(0, len(full) - len(visible)),
            visible_tool_schema_tokens=visible_tokens,
            full_tool_schema_tokens=full_tokens,
            estimated_tool_schema_tokens_saved=max(
                0,
                full_tokens - visible_tokens,
            ),
            visible_tool_names=[
                str(spec.get("name") or "") for spec in visible
            ],
        )

    def _self_directed_mcp_server_groups(self) -> dict[str, list[dict[str, Any]]]:
        patterns = self.config.autonomy.allowed_tools
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in sorted(
            self.mcp.tool_specs,
            key=lambda item: str(item.get("name") or ""),
        ):
            if not any(
                fnmatch.fnmatchcase(str(spec["name"]), pattern)
                for pattern in patterns
            ):
                continue
            server = self._mcp_tool_group(str(spec.get("name") or ""))
            groups.setdefault(server, []).append(spec)
        return dict(sorted(groups.items()))

    def _heartbeat_mcp_server_catalog(self) -> list[dict[str, object]]:
        return [
            {
                "id": server,
                "description": self._mcp_group_description(server),
            }
            for server in self._self_directed_mcp_server_groups()
        ]

    def _heartbeat_external_tool_specs(
        self,
        plan: dict[str, object],
    ) -> list[dict[str, Any]]:
        patterns = self.config.autonomy.allowed_tools
        internal = [
            spec
            for spec in SELF_DIRECTED_BUILTIN_TOOL_SPECS
            if any(
                fnmatch.fnmatchcase(str(spec["name"]), pattern)
                for pattern in patterns
            )
        ]
        groups = self._self_directed_mcp_server_groups()
        handoff = plan.get("heartbeat_handoff")
        route = handoff.get("mcp") if isinstance(handoff, dict) else None
        selected = route.get("servers") if isinstance(route, dict) else []
        selected_servers = selected if isinstance(selected, list) else []
        mcp_specs = [
            spec
            for server in selected_servers
            for spec in groups.get(str(server), [])
        ]
        group_catalog = {
            server: self._mcp_group_description(server)
            for server in groups
        }
        return [*internal, *mcp_specs, tool_enable_spec(group_catalog)]

    def _owner_tool_specs(
        self, plan: dict[str, object], channel_name: str | None = None
    ) -> list[dict[str, Any]]:
        mcp_groups = self._mcp_server_groups()
        handoff = plan.get("owner_handoff")
        route = handoff.get("mcp") if isinstance(handoff, dict) else None
        raw_selected = route.get("servers") if isinstance(route, dict) else []
        selected = (
            [
                str(group)
                for group in raw_selected
                if str(group) in mcp_groups
            ]
            if isinstance(raw_selected, list)
            else []
        )
        optional = [
            spec
            for group in selected
            for spec in mcp_groups.get(group, [])
        ]
        all_internal = self._owner_internal_tool_surface()
        group_catalog = {
            group: self._mcp_group_description(group)
            for group in mcp_groups
        }
        visible = [
            self._send_message_tool_spec(channel_name),
            *all_internal,
            tool_enable_spec(group_catalog),
            *optional,
            owner_end_turn_tool_spec(),
        ]
        full = [
            self._send_message_tool_spec(channel_name),
            *all_internal,
            tool_enable_spec(group_catalog),
            *[
                spec
                for specs in mcp_groups.values()
                for spec in specs
            ],
            owner_end_turn_tool_spec(),
        ]
        self._log_owner_tool_projection(
            visible=visible,
            full=full,
        )
        return visible

    async def _append_owner_updates(
        self,
        updates: list[IncomingMessage],
        current_events: list[IncomingMessage],
        turn_id: str,
        authority: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        delivery_channel: Channel,
    ) -> tuple[list[dict[str, Any]], str]:
        context_plan, recalled = await self._prepare_owner_context(
            current_events, turn_id
        )
        if authority == "owner":
            self._append_visible_tool_specs(
                tools, self._owner_tool_specs(context_plan, delivery_channel.name)
            )
        messages.append(
            self._owner_update_message(
                updates, delivery_channel, context_plan, recalled
            )
        )
        return tools, updates[-1].event_id

    async def _run_tool_loop(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        current_events: list[IncomingMessage],
        draft: TurnDraft,
        *,
        authority: str,
        source_event_id: str,
        allow_notify: bool,
        turn_id: str,
        require_response: bool,
        autonomous_goal_id: str | None = None,
        heartbeat_turn: bool = False,
        reply_wait_turn: bool = False,
        heartbeat_owner_event_revision: int | None = None,
        heartbeat_notification_key: str = "heartbeat.chat",
        allowed_capabilities: set[str] | None = None,
        artifact_root: Path | None = None,
        accept_owner_updates: bool = False,
        dynamic_tool_policies: bool = False,
        delivery_channel: Channel,
    ) -> AgentReply | dict[str, Any] | None:
        external_tool_used = False
        force_autonomous_finish = False
        failed_tool_rounds = 0
        history_messages = max(0, len(messages) - 1)
        visible_since_owner_update = False
        owner_work_acknowledged = False
        llm_round = 0
        enable_tool_groups = (
            self._owner_enable_tool_groups()
            if authority == "owner"
            else (
                self._self_directed_mcp_server_groups()
                if heartbeat_turn
                else {}
            )
        )
        stage = (
            "reply_followup"
            if reply_wait_turn
            else (
                "heartbeat"
                if heartbeat_turn
                else ("goal" if autonomous_goal_id else authority)
            )
        )
        while True:
            if reply_wait_turn and self.store.pending_owner_reply() is None:
                return None
            updates = (
                await self._settle_owner_updates(current_events, delivery_channel.name)
                if accept_owner_updates
                else []
            )
            if updates:
                visible_since_owner_update = False
                owner_work_acknowledged = False
                tools, source_event_id = await self._append_owner_updates(
                    updates,
                    current_events,
                    turn_id,
                    authority,
                    tools,
                    messages,
                    delivery_channel,
                )
                force_autonomous_finish = False
                failed_tool_rounds = 0
            request_tools = (
                [AUTONOMOUS_FINISH_SPEC] if force_autonomous_finish else tools
            )
            request_system = (
                self._system_with_tool_policies(system, request_tools)
                if dynamic_tool_policies
                else system
            )
            llm_round += 1
            call_id = new_trace_id()
            with log_context(
                stage=stage,
                turn_id=turn_id,
                call_id=call_id,
                round=llm_round,
                channel=delivery_channel.name,
                goal_id=autonomous_goal_id,
            ):
                history_messages = self._fit_context(
                    request_system, messages, request_tools, history_messages
                )
                self._check_turn_budget(
                    turn_id, request_system, messages, request_tools
                )
            require_tool = bool(
                autonomous_goal_id or heartbeat_turn or reply_wait_turn
            ) or (
                require_response and self.config.llm.api_format == "openai"
            )
            try:
                with log_context(
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    channel=delivery_channel.name,
                    goal_id=autonomous_goal_id,
                ):
                    if accept_owner_updates:
                        response = await self._complete_with_owner_interrupt(
                            request_system,
                            messages,
                            request_tools,
                            require_tool=require_tool,
                            current_events=current_events,
                            channel_name=delivery_channel.name,
                        )
                    else:
                        response = await self.provider.complete(
                            request_system,
                            messages,
                            request_tools,
                            require_tool=require_tool,
                        )
            except OwnerMessagesChanged as interruption:
                updates = list(interruption.updates)
                updates.extend(
                    await self._settle_owner_updates(
                        current_events, delivery_channel.name
                    )
                )
                visible_since_owner_update = False
                owner_work_acknowledged = False
                tools, source_event_id = await self._append_owner_updates(
                    updates,
                    current_events,
                    turn_id,
                    authority,
                    tools,
                    messages,
                    delivery_channel,
                )
                force_autonomous_finish = False
                failed_tool_rounds = 0
                continue
            except Exception as error:
                if external_tool_used:
                    raise ExternalToolTurnError(type(error).__name__) from error
                raise
            metrics = response.usage or {}
            input_tokens = int(
                metrics.get(
                    "input",
                    estimate_tokens(
                        json.dumps(
                            {
                                "system": request_system,
                                "messages": messages,
                                "tools": request_tools,
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                )
            )
            output_tokens = int(
                metrics.get(
                    "output",
                    estimate_tokens(
                        json.dumps(response.content, ensure_ascii=False, default=str)
                    ),
                )
            )
            self.store.record_turn_usage(turn_id, input_tokens, output_tokens)
            updates = (
                await self._settle_owner_updates(current_events, delivery_channel.name)
                if accept_owner_updates
                else []
            )
            if updates:
                visible_since_owner_update = False
                owner_work_acknowledged = False
                messages.append({"role": "assistant", "content": response.content})
                if response.tool_calls:
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                _tool_error_block(
                                    call.id, "superseded_by_owner_update"
                                )
                                for call in response.tool_calls
                            ],
                        }
                    )
                tools, source_event_id = await self._append_owner_updates(
                    updates,
                    current_events,
                    turn_id,
                    authority,
                    tools,
                    messages,
                    delivery_channel,
                )
                force_autonomous_finish = False
                failed_tool_rounds = 0
                continue
            if not response.tool_calls:
                if autonomous_goal_id:
                    messages.extend(
                        [
                            {"role": "assistant", "content": response.content},
                            {
                                "role": "user",
                                "content": (
                                    "[Trusted runtime protocol error. Plain text was not "
                                    "stored. Finish now by calling autonomous_finish alone.]"
                                ),
                            },
                        ]
                    )
                    force_autonomous_finish = True
                    continue
                if not require_response:
                    return None
                log_event(
                    logger,
                    logging.DEBUG,
                    "llm_protocol_rejected",
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    reason="end_turn_tool_required",
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": (
                                "[Trusted runtime protocol error. The previous text was not "
                                "delivered. In this response, call send_message with the "
                                "owner-visible reply; do not output assistant text or call "
                                "end_turn. After the send_message result, call end_turn "
                                "alone in a later response.]"
                            ),
                        },
                    ]
                )
                continue
            if (
                autonomous_goal_id
                and len(response.tool_calls) == 1
                and response.tool_calls[0].name == "autonomous_finish"
            ):
                if autonomous_goal_id in draft.goals:
                    return None
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": [
                                _tool_error_block(
                                    response.tool_calls[0].id,
                                    "goal_must_be_updated_before_finish",
                                )
                            ],
                        },
                    ]
                )
                force_autonomous_finish = False
                continue
            if (
                require_response
                and len(response.tool_calls) == 1
                and response.tool_calls[0].name == "end_turn"
            ):
                plain_text = response_text(response.content)
                log_event(
                    logger,
                    TRACE,
                    "end_turn_received",
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    channel=delivery_channel.name,
                    arguments=safe_preview(response.tool_calls[0].arguments, 1000),
                )
                reply, error = parse_response(
                    response.tool_calls[0].arguments,
                    require_heartbeat=heartbeat_turn,
                    allow_activity_update=authority == "owner",
                )
                if plain_text:
                    reply = None
                    error = "plain_text_with_end_turn"
                if reply is not None and heartbeat_turn and reply.heartbeat:
                    minutes = int(reply.heartbeat["next_check_minutes"])
                    seconds = minutes * 60
                    if not (
                        self.config.heartbeat.min_interval_seconds
                        <= seconds
                        <= self.config.heartbeat.max_interval_seconds
                    ):
                        reply = None
                        error = "heartbeat_interval_out_of_range"
                if reply is not None:
                    error = self._validate_emotion_messages(reply.messages)
                    if error is not None:
                        reply = None
                if (
                    reply is not None
                    and reply.expects_reply
                    and not reply.messages
                    and not visible_since_owner_update
                ):
                    reply = None
                    error = "reply_expectation_without_visible_message"
                if (
                    reply is not None
                    and reply_wait_turn
                    and not visible_since_owner_update
                ):
                    reply = None
                    error = "reply_followup_message_required"
                if (
                    reply is not None
                    and reply_wait_turn
                    and reply.should_schedule_reply_wait
                ):
                    reply = None
                    error = "reply_followup_cannot_schedule_another_wait"
                if reply is not None:
                    log_event(
                        logger,
                        TRACE,
                        "end_turn_accepted",
                        stage=stage,
                        turn_id=turn_id,
                        call_id=call_id,
                        round=llm_round,
                        mood_decision=(
                            "updated" if reply.mood_update else "unchanged"
                        ),
                    )
                    return reply
                log_event(
                    logger,
                    TRACE,
                    "end_turn_rejected",
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    reason=error,
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": (
                                [
                                    _tool_error_block(
                                        response.tool_calls[0].id, error
                                    ),
                                    {
                                        "type": "text",
                                        "text": (
                                            "[Trusted runtime protocol correction: "
                                            "plain assistant text is not delivered. "
                                            "In this response, call send_message with "
                                            "the owner-visible reply and do not call "
                                            "end_turn. After its result, call end_turn "
                                            "alone in a later response.]"
                                        ),
                                    },
                                ]
                                if error == "plain_text_with_end_turn"
                                else [
                                    _tool_error_block(
                                        response.tool_calls[0].id, error
                                    )
                                ]
                            ),
                        },
                    ]
                )
                continue
            missing_announce = self._missing_initial_work_announce(
                response.tool_calls,
                request_tools,
                owner_work_acknowledged=owner_work_acknowledged,
                deliver=should_deliver_announce(
                    heartbeat_turn=heartbeat_turn,
                    reply_wait_turn=reply_wait_turn,
                    autonomous_goal=bool(autonomous_goal_id),
                ),
            )
            if missing_announce is not None:
                missing_call_id, field = missing_announce
                messages.append(
                    {"role": "assistant", "content": copy.deepcopy(response.content)}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            (
                                _tool_result_block(
                                    call.id,
                                    {
                                        "ok": False,
                                        "error": "owner_work_acknowledgement_required",
                                        "message": initial_announce_error_message(field),
                                    },
                                )
                                if call.id == missing_call_id
                                else _tool_error_block(
                                    call.id,
                                    "superseded_by_owner_work_acknowledgement",
                                )
                            )
                            for call in response.tool_calls
                        ],
                    }
                )
                failed_tool_rounds += 1
                if failed_tool_rounds >= MAX_CONSECUTIVE_TOOL_FAILURES:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[Trusted runtime protocol stop. Tool calls failed "
                                "validation three consecutive times. Do not retry tools "
                                "in this Turn. In this response, use send_message for "
                                "the last concrete failure reason without end_turn. "
                                "After its result, call end_turn alone in a later response.]"
                            ),
                        }
                    )
                continue
            # Tool execution strips harness-only arguments in place. Preserve an
            # independent assistant-history copy so the next model round still
            # knows exactly what the owner already heard.
            assistant_history_content = copy.deepcopy(response.content)
            if response.reasoning and response.tool_calls:
                assistant_history_content.insert(
                    0,
                    {
                        "type": "reasoning",
                        "text": response.reasoning,
                    },
                )
            messages.append(
                {"role": "assistant", "content": assistant_history_content}
            )
            history_tool_inputs = {
                str(block.get("id") or ""): block.get("input")
                for block in (
                    assistant_history_content
                    if isinstance(assistant_history_content, list)
                    else []
                )
                if isinstance(block, dict)
                and block.get("type") == "tool_use"
                and isinstance(block.get("input"), dict)
            }
            results: list[dict[str, Any]] = []
            updates = []
            allowed_tool_names = {str(spec["name"]) for spec in request_tools}
            announce_delivered_in_batch = False
            for index, call in enumerate(response.tool_calls):
                tool_started = time.monotonic()
                source = self._tool_source(call.name, allow_notify=allow_notify)
                journal_tool = source in {"mcp", "builtin", "agenda", "memory"}
                if journal_tool:
                    journal_arguments = dict(call.arguments)
                    journal_arguments.pop("say_to_owner", None)
                    self._journal_turn_item(
                        turn_id,
                        "tool_call",
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "source": source,
                            "arguments": compact_log_value(
                                journal_arguments,
                                string_limit=500,
                                item_limit=20,
                            ),
                        },
                        trust="runtime",
                    )
                log_event(
                    logger,
                    logging.DEBUG,
                    "tool_start",
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    channel=delivery_channel.name,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments=compact_log_value(
                        call.arguments,
                        string_limit=800,
                        item_limit=30,
                    ),
                )
                if call.argument_error:
                    result = {
                        "ok": False,
                        "error": call.argument_error,
                        "message": (
                            "Tool arguments must be one valid JSON object. "
                            "Call the tool again with corrected arguments."
                        ),
                    }
                elif call.name not in allowed_tool_names:
                    result = {"ok": False, "error": "tool_not_allowed"}
                elif call.name == "end_turn":
                    result = {
                        "ok": False,
                        "error": (
                            "end_turn_must_be_the_only_terminal_tool"
                            if require_response
                            else "tool_not_allowed"
                        ),
                    }
                elif call.name == "autonomous_finish":
                    result = {
                        "ok": False,
                        "error": "autonomous_finish_must_be_the_only_terminal_tool",
                    }
                elif call.name == "send_message":
                    progress, error = parse_messages(call.arguments)
                    if progress is not None:
                        error = self._validate_emotion_messages(progress)
                        if error is not None:
                            progress = None
                    if not (require_response or heartbeat_turn):
                        result = {"ok": False, "error": "tool_not_allowed"}
                    elif not call.id:
                        result = {"ok": False, "error": "missing_tool_call_id"}
                    elif progress is None:
                        result = {"ok": False, "error": error}
                    else:
                        check_contact = (
                            (heartbeat_turn or reply_wait_turn)
                            and heartbeat_owner_event_revision is not None
                        )
                        contact_error = (
                            self._heartbeat_contact_error(
                                heartbeat_owner_event_revision,
                                heartbeat_notification_key,
                            )
                            if check_contact
                            else None
                        )
                        if contact_error is not None:
                            result = {"ok": False, "error": contact_error}
                        else:
                            target = self.channels.get(
                                str(
                                    call.arguments.get("channel")
                                    or delivery_channel.name
                                )
                            )
                            if target is None:
                                result = {"ok": False, "error": "invalid_channel"}
                            else:
                                self.store.queue_progress(
                                    turn_id, call.id, progress, target.name
                                )
                                visible_since_owner_update = True
                                if not check_contact:
                                    owner_work_acknowledged = True
                                self.outbox_changed.set()
                                result = {
                                    "ok": True,
                                    "state": "queued",
                                    "channel": target.name,
                                    "messages": len(progress),
                                }
                elif call.name == "tool_enable":
                    requested = call.arguments.get("groups")
                    if (
                        not isinstance(requested, list)
                        or not requested
                        or any(
                            not isinstance(group, str)
                            or group not in enable_tool_groups
                            for group in requested
                        )
                    ):
                        result = {
                            "ok": False,
                            "error": "invalid_tool_groups",
                        }
                    else:
                        enabled_tools = self._append_visible_tool_specs(
                            tools,
                            [
                                spec
                                for group in dict.fromkeys(requested)
                                for spec in enable_tool_groups[group]
                            ],
                        )
                        result = {
                            "ok": True,
                            "state": "enabled",
                            "groups": list(dict.fromkeys(requested)),
                            "tools": enabled_tools,
                        }
                elif self.mcp.has_tool(call.name) or self.builtin_tools.has_tool(
                    call.name
                ):
                    result = None
                    if not call.id:
                        result = {"ok": False, "error": "missing_tool_call_id"}
                    else:
                        announce = next(
                            (
                                announce_field(spec)
                                for spec in request_tools
                                if spec.get("name") == call.name
                            ),
                            None,
                        )
                        if announce:
                            text, _ = apply_tool_announce(
                                call.arguments,
                                announce,
                                deliver=should_deliver_announce(
                                    heartbeat_turn=heartbeat_turn,
                                    reply_wait_turn=reply_wait_turn,
                                    autonomous_goal=bool(autonomous_goal_id),
                                )
                                and not announce_delivered_in_batch,
                            )
                            history_arguments = history_tool_inputs.get(call.id)
                            if isinstance(history_arguments, dict):
                                if text is None:
                                    history_arguments.pop(announce, None)
                                else:
                                    history_arguments[announce] = text
                            if text is not None:
                                self.store.queue_progress(
                                    turn_id,
                                    call.id,
                                    [text],
                                    self.channel.name,
                                )
                                announce_delivered_in_batch = True
                                visible_since_owner_update = True
                                owner_work_acknowledged = True
                                self.outbox_changed.set()
                    if result is None:
                        capability = (
                            self.mcp.capability(call.name)
                            if source == "mcp"
                            else self.builtin_tools.capability(call)
                        )
                        if (
                            allowed_capabilities is not None
                            and capability not in allowed_capabilities
                        ):
                            result = {"ok": False, "error": "tool_not_allowed"}
                        elif (
                            artifact_root is not None
                            and call.name in {"read_file", "write_file", "list_dir"}
                            and not self._artifact_path_allowed(call, artifact_root)
                        ):
                            result = {
                                "ok": False,
                                "error": "path_outside_autonomous_artifacts",
                            }
                        else:
                            external_tool_used = (
                                external_tool_used or capability != "read"
                            )
                            result = self.store.begin_tool_call(
                                turn_id,
                                call.id,
                                call.name,
                                call.arguments,
                                capability,
                            )
                            if result is None:
                                with log_context(
                                    stage=stage,
                                    turn_id=turn_id,
                                    call_id=call_id,
                                    round=llm_round,
                                    channel=delivery_channel.name,
                                    goal_id=autonomous_goal_id,
                                    tool_call_id=call.id,
                                    tool_name=call.name,
                                ):
                                    result = (
                                        await self.mcp.call(call.name, call.arguments)
                                        if self.mcp.has_tool(call.name)
                                        else await self.builtin_tools.execute(call)
                                    )
                                result = self._normalize_tool_result(
                                    call, result, source
                                )
                                self.store.complete_tool_call(turn_id, call.id, result)
                elif self.agenda_tools.has_tool(call.name, allow_notify=allow_notify):
                    result = self.agenda_tools.execute(
                        call,
                        draft,
                        authority=authority,
                        source_event_id=source_event_id,
                        allow_notify=allow_notify,
                    )
                else:
                    result = self.memory_tools.execute(call, current_events, draft)
                if "provenance" not in result:
                    result = self._normalize_tool_result(call, result, source)
                provenance = result.get("provenance")
                log_message = (
                    result.get("message")
                    if isinstance(provenance, dict)
                    and provenance.get("source") in {"agenda", "memory", "runtime"}
                    else None
                )
                log_event(
                    logger,
                    logging.DEBUG,
                    "tool_end",
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    channel=delivery_channel.name,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    ok=bool(result.get("ok")),
                    error=result.get("error"),
                    result=compact_log_value(
                        result,
                        string_limit=800,
                        item_limit=30,
                    ),
                    result_message=(
                        safe_preview(log_message, 500)
                        if log_message is not None
                        else None
                    ),
                    duration_ms=int((time.monotonic() - tool_started) * 1000),
                )
                draft.tool_calls.append(
                    {
                        "tool": call.name,
                        "ok": bool(result.get("ok")),
                        "error": result.get("error"),
                        "duration_ms": int(
                            (time.monotonic() - tool_started) * 1000
                        ),
                    }
                )
                if journal_tool:
                    self._journal_turn_item(
                        turn_id,
                        "tool_result",
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "ok": bool(result.get("ok")),
                            "error": result.get("error"),
                            "result": compact_log_value(
                                result,
                                string_limit=800,
                                item_limit=30,
                            ),
                        },
                        trust=(
                            "untrusted_tool_data"
                            if source in {"mcp", "builtin"}
                            else "runtime"
                        ),
                    )
                results.append(_tool_result_block(call.id, result))
                if accept_owner_updates:
                    updates = await self._settle_owner_updates(
                        current_events, delivery_channel.name
                    )
                    if updates:
                        results.extend(
                            _tool_error_block(
                                pending.id, "superseded_by_owner_update"
                            )
                            for pending in response.tool_calls[index + 1 :]
                        )
                        break
            messages.append({"role": "user", "content": results})
            if updates:
                visible_since_owner_update = False
                tools, source_event_id = await self._append_owner_updates(
                    updates,
                    current_events,
                    turn_id,
                    authority,
                    tools,
                    messages,
                    delivery_channel,
                )
                failed_tool_rounds = 0
                continue
            if any(not block["is_error"] for block in results):
                failed_tool_rounds = 0
                continue
            failed_tool_rounds += 1
            if failed_tool_rounds < MAX_CONSECUTIVE_TOOL_FAILURES:
                continue
            if not require_response:
                raise RuntimeError("repeated tool validation failures")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[Trusted runtime protocol stop. Tool calls failed validation "
                        "three consecutive times. Do not retry tools in this Turn. "
                        "In this response, use send_message for the last concrete "
                        "failure reason without end_turn. After its result, call "
                        "end_turn alone in a later response.]"
                    ),
                }
            )

    @staticmethod
    def _missing_initial_work_announce(
        calls: list[ToolCall],
        request_tools: list[dict[str, Any]],
        *,
        owner_work_acknowledged: bool,
        deliver: bool,
    ) -> tuple[str, str] | None:
        if owner_work_acknowledged or not deliver:
            return None
        announce_fields = {
            str(spec.get("name") or ""): announce_field(spec)
            for spec in request_tools
        }
        for index, call in enumerate(calls):
            field = announce_fields.get(call.name)
            if not field:
                continue
            if any(
                earlier.name == "send_message"
                and bool(earlier.arguments.get("messages"))
                for earlier in calls[:index]
            ):
                return None
            if str(call.arguments.get(field) or "").strip():
                return None
            return call.id, field
        return None

    def _journal_turn_item(
        self,
        turn_id: str,
        item_type: str,
        payload: dict[str, object],
        *,
        trust: str,
    ) -> None:
        try:
            self.store.append_turn_journal(
                turn_id,
                item_type,
                payload,
                visibility="internal",
                trust=trust,
            )
        except Exception:
            log_event(
                logger,
                logging.WARNING,
                "turn_journal_failed",
                turn_id=turn_id,
                item_type=item_type,
                exc_info=True,
            )

    def _tool_source(self, name: str, *, allow_notify: bool) -> str:
        if name in {
            "end_turn",
            "send_message",
            "tool_enable",
            "autonomous_finish",
        }:
            return "runtime"
        if self.mcp.has_tool(name):
            return "mcp"
        if self.builtin_tools.has_tool(name):
            return "builtin"
        if self.agenda_tools.has_tool(name, allow_notify=allow_notify):
            return "agenda"
        if name in {str(spec["name"]) for spec in MEMORY_TOOL_SPECS}:
            return "memory"
        return "unknown"

    def _normalize_tool_result(
        self, call: ToolCall, result: object, source: str
    ) -> ToolResult:
        raw = dict(result) if isinstance(result, dict) else {"value": result}
        ok = raw.get("ok") is True
        error = None if ok else str(raw.get("error") or "tool_failed")
        payload = {
            key: value
            for key, value in raw.items()
            if key not in {"ok", "error", "truncated", "provenance"}
        }
        provenance = {"source": source, "tool": call.name}
        envelope: dict[str, Any] = {
            "ok": ok,
            "error": error,
            "truncated": bool(raw.get("truncated", False)),
            "provenance": provenance,
            **payload,
        }
        serialized = json.dumps(envelope, ensure_ascii=False, default=str)
        if len(serialized) <= self.config.tool_result_max_chars:
            return envelope
        message = raw.get("message")
        compact_payload = {
            key: value for key, value in payload.items() if key != "message"
        }
        payload_text = json.dumps(
            compact_payload, ensure_ascii=False, default=str
        )
        truncated = {
            "ok": ok,
            "error": error,
            "truncated": True,
            "provenance": provenance,
            "original_chars": len(serialized),
            "content": payload_text[: self.config.tool_result_max_chars],
        }
        if message is not None:
            truncated["message"] = safe_preview(message, 1000)
        return truncated

    def _artifact_path_allowed(self, call: ToolCall, root: Path) -> bool:
        try:
            self.builtin_tools.resolve_path(
                call.arguments.get("path")
            ).relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _announced_tool_specs(
        self, specs: list[dict[str, Any]], *, mcp: bool
    ) -> list[dict[str, Any]]:
        return [
            decorate_tool_spec(spec)
            if should_announce(str(spec.get("name") or ""), mcp=mcp)
            else spec
            for spec in specs
        ]

    def _self_directed_tool_specs(self) -> list[dict[str, Any]]:
        patterns = self.config.autonomy.allowed_tools
        return [
            spec
            for spec in [
                *SELF_DIRECTED_BUILTIN_TOOL_SPECS,
                *self.mcp.tool_specs,
            ]
            if any(
                fnmatch.fnmatchcase(str(spec["name"]), pattern)
                for pattern in patterns
            )
        ]

    def _send_message_tool_spec(
        self, channel_name: str | None = None
    ) -> dict[str, Any]:
        return send_message_tool_spec(
            list(self.channels), channel_name or self.channel.name
        )

    def _heartbeat_contact_error(
        self, owner_event_revision: int, notification_key: str
    ) -> str | None:
        snapshot = self.store.heartbeat_conversation_snapshot()
        if int(snapshot["owner_event_revision"]) != owner_event_revision:
            return "heartbeat_superseded_by_owner_update"
        if snapshot["owner_busy"]:
            return "heartbeat_contact_unavailable"
        window = self.store.heartbeat_contact_window(
            notification_key,
            self.config.notifications,
            apply_cooldown=notification_key != "heartbeat.reply_followup",
        )
        return None if window["allowed"] else "heartbeat_contact_unavailable"

    def _artifact_root(self) -> Path:
        return Path(self.config.workspace or self.config.database.parent) / "artifacts"

    def _check_turn_budget(
        self,
        turn_id: str,
        system: object,
        messages: object,
        tools: object,
    ) -> None:
        usage = self.store.turn_usage(turn_id)
        elapsed = time.time() - float(usage["started_at"])
        if self.config.turn_max_seconds and elapsed >= self.config.turn_max_seconds:
            raise TurnBudgetExceeded("time limit reached")
        estimated_input = estimate_tokens(
            json.dumps(
                {"system": system, "messages": messages, "tools": tools},
                ensure_ascii=False,
                default=str,
            )
        )
        total = int(usage["input"]) + int(usage["output"])
        if (
            self.config.turn_max_total_tokens
            and total + estimated_input > self.config.turn_max_total_tokens
        ):
            raise TurnBudgetExceeded("token limit reached")

    def _fit_context(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        history_messages: int,
    ) -> int:
        def size() -> int:
            return estimate_tokens(
                json.dumps(
                    {"system": system, "messages": messages, "tools": tools},
                    ensure_ascii=False,
                    default=str,
                )
            )

        estimated = size()
        dropped = 0
        truncated = 0
        compression_breakers = 0
        # ponytail: repeated estimation is fine at single-user scale; profile before optimizing.
        while estimated > self.config.max_input_tokens and history_messages:
            messages.pop(0)
            history_messages -= 1
            dropped += 1
            estimated = size()
        if estimated > self.config.max_input_tokens:
            for message in messages:
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        estimated <= self.config.max_input_tokens
                        or not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    result = block.get("content")
                    attempts = 0
                    while (
                        isinstance(result, str)
                        and len(result) > 1000
                        and estimated > self.config.max_input_tokens
                    ):
                        if attempts >= MAX_TOOL_RESULT_TRUNCATION_ATTEMPTS:
                            compression_breakers += 1
                            log_event(
                                logger,
                                logging.WARNING,
                                "tool_result_truncation_stalled",
                                reason="attempt_limit",
                                attempts=attempts,
                                result_chars=len(result),
                                estimated_input=estimated,
                                input_limit=self.config.max_input_tokens,
                            )
                            break
                        attempts += 1
                        candidate = _truncate_tool_result_json(
                            result, max(1000, len(result) // 2)
                        )
                        if len(candidate) >= len(result):
                            compression_breakers += 1
                            log_event(
                                logger,
                                logging.WARNING,
                                "tool_result_truncation_stalled",
                                reason="non_shrinking_result",
                                attempts=attempts,
                                before_chars=len(result),
                                after_chars=len(candidate),
                                estimated_input=estimated,
                                input_limit=self.config.max_input_tokens,
                            )
                            break
                        before_estimated = estimated
                        block["content"] = candidate
                        candidate_estimated = size()
                        if candidate_estimated >= before_estimated:
                            block["content"] = result
                            compression_breakers += 1
                            log_event(
                                logger,
                                logging.WARNING,
                                "tool_result_truncation_stalled",
                                reason="non_shrinking_input",
                                attempts=attempts,
                                before_chars=len(result),
                                after_chars=len(candidate),
                                before_estimated=before_estimated,
                                after_estimated=candidate_estimated,
                                input_limit=self.config.max_input_tokens,
                            )
                            break
                        result = candidate
                        estimated = candidate_estimated
                        truncated += 1
        log_event(
            logger,
            TRACE,
            "llm_context_fit",
            estimated_input=estimated,
            input_limit=self.config.max_input_tokens,
            history_dropped=dropped,
            tool_results_truncated=truncated,
            compression_breakers=compression_breakers,
        )
        if estimated > self.config.max_input_tokens:
            log_event(
                logger,
                logging.WARNING,
                "llm_context_oversize",
                estimated_input=estimated,
                input_limit=self.config.max_input_tokens,
                single_turn_context=history_messages == 0,
                proceeding=True,
                history_dropped=dropped,
                compression_breakers=compression_breakers,
            )
        return history_messages

    def _validate_emotion_messages(self, messages: list[ChannelMessage]) -> str | None:
        for message in messages:
            if not isinstance(message, str):
                continue
            if not message.startswith(EMOTION_PREFIX):
                continue
            slug = emotion_slug(message)
            if slug is None:
                return "invalid_emotion_directive"
            if self.store.emotion(slug) is None:
                return "unknown_emotion_slug"
        return None
