import fnmatch
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..agenda_tools import AGENDA_TOOL_SPECS
from ..builtin_tools import BUILTIN_TOOL_SPECS, SELF_DIRECTED_BUILTIN_TOOL_SPECS
from ..channel import Channel, ChannelMessage
from ..emotions import EMOTION_PREFIX, emotion_slug
from ..logging_context import TRACE, compact_log_value, log_context, log_event, new_trace_id, safe_preview
from ..memory_tools import MEMORY_TOOL_SPECS
from ..models import AgentReply, IncomingMessage, ToolCall, TurnDraft
from ..storage import estimate_tokens
from .protocol import (
    AUTONOMOUS_FINISH_SPEC,
    REPLY_EXPECTATION_CLOSE_SPEC,
    RESPOND_TOOL_SPEC,
    heartbeat_respond_tool_spec,
    reply_wait_respond_tool_spec,
    send_message_tool_spec,
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

class ToolExecutionService:
    def _owner_tool_specs(
        self, plan: dict[str, object], channel_name: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            self._send_message_tool_spec(channel_name),
            *MEMORY_TOOL_SPECS,
            *AGENDA_TOOL_SPECS,
            *BUILTIN_TOOL_SPECS,
            *self.mcp.tool_specs,
            REPLY_EXPECTATION_CLOSE_SPEC,
            RESPOND_TOOL_SPEC,
        ]

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
        force_response = False
        force_autonomous_finish = False
        failed_tool_rounds = 0
        history_messages = max(0, len(messages) - 1)
        visible_since_owner_update = False
        llm_round = 0
        stage = (
            "reply_wait"
            if reply_wait_turn
            else (
                "heartbeat"
                if heartbeat_turn
                else ("goal" if autonomous_goal_id else authority)
            )
        )
        while True:
            updates = (
                await self._settle_owner_updates(current_events, delivery_channel.name)
                if accept_owner_updates
                else []
            )
            if updates:
                visible_since_owner_update = False
                context_plan, recalled = await self._prepare_owner_context(
                    current_events, turn_id
                )
                if authority == "owner":
                    tools = self._owner_tool_specs(context_plan, delivery_channel.name)
                messages.append(
                    self._owner_update_message(
                        updates, delivery_channel, context_plan, recalled
                    )
                )
                source_event_id = updates[-1].event_id
                force_response = False
                force_autonomous_finish = False
                failed_tool_rounds = 0
            terminal_tool = (
                reply_wait_respond_tool_spec()
                if reply_wait_turn
                else (
                    heartbeat_respond_tool_spec()
                    if heartbeat_turn
                    else RESPOND_TOOL_SPEC
                )
            )
            request_tools = (
                [self._send_message_tool_spec(delivery_channel.name), terminal_tool]
                if force_response
                else ([AUTONOMOUS_FINISH_SPEC] if force_autonomous_finish else tools)
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
                context_plan, recalled = await self._prepare_owner_context(
                    current_events, turn_id
                )
                if authority == "owner":
                    tools = self._owner_tool_specs(
                        context_plan, delivery_channel.name
                    )
                messages.append(
                    self._owner_update_message(
                        updates, delivery_channel, context_plan, recalled
                    )
                )
                source_event_id = updates[-1].event_id
                force_response = False
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
                context_plan, recalled = await self._prepare_owner_context(
                    current_events, turn_id
                )
                if authority == "owner":
                    tools = self._owner_tool_specs(context_plan, delivery_channel.name)
                messages.append(
                    self._owner_update_message(
                        updates, delivery_channel, context_plan, recalled
                    )
                )
                source_event_id = updates[-1].event_id
                force_response = False
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
                    reason="respond_tool_required",
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": (
                                "[Trusted runtime protocol error. The previous text was not "
                                "delivered. Send any owner-visible reply with send_message, "
                                "then finish by calling respond for the Turn state. Do not "
                                "output plain assistant text.]"
                            ),
                        },
                    ]
                )
                force_response = True
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
                and response.tool_calls[0].name == "respond"
            ):
                plain_text = self._context_plan_response_text(response.content)
                log_event(
                    logger,
                    TRACE,
                    "respond_received",
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    channel=delivery_channel.name,
                    arguments=safe_preview(response.tool_calls[0].arguments, 1000),
                )
                reply, error = self._parse_response(
                    response.tool_calls[0].arguments,
                    require_heartbeat=heartbeat_turn,
                    require_reply_wait=reply_wait_turn,
                )
                if reply is not None and plain_text:
                    reply = None
                    error = "plain_text_with_respond"
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
                if reply is not None:
                    log_event(
                        logger,
                        TRACE,
                        "respond_accepted",
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
                    "respond_rejected",
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
                                            "Send any owner-visible reply with "
                                            "send_message first, then call respond "
                                            "alone to finish.]"
                                        ),
                                    },
                                ]
                                if error == "plain_text_with_respond"
                                else [
                                    _tool_error_block(
                                        response.tool_calls[0].id, error
                                    )
                                ]
                            ),
                        },
                    ]
                )
                force_response = True
                continue
            messages.append({"role": "assistant", "content": response.content})
            results: list[dict[str, Any]] = []
            updates = []
            allowed_tool_names = {str(spec["name"]) for spec in request_tools}
            for index, call in enumerate(response.tool_calls):
                tool_started = time.monotonic()
                source = self._tool_source(call.name, allow_notify=allow_notify)
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
                elif call.name == "respond":
                    result = {
                        "ok": False,
                        "error": (
                            "respond_must_be_the_only_terminal_tool"
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
                    progress, error = self._parse_messages(call.arguments)
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
                    elif (
                        (heartbeat_turn or reply_wait_turn)
                        and heartbeat_owner_event_revision is not None
                    ):
                        contact_error = self._heartbeat_contact_error(
                            heartbeat_owner_event_revision,
                            heartbeat_notification_key,
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
                                self.outbox_changed.set()
                                result = {
                                    "ok": True,
                                    "state": "queued",
                                    "channel": target.name,
                                    "messages": len(progress),
                                }
                    else:
                        target = self.channels.get(
                            str(call.arguments.get("channel") or delivery_channel.name)
                        )
                        if target is None:
                            result = {"ok": False, "error": "invalid_channel"}
                        else:
                            self.store.queue_progress(
                                turn_id, call.id, progress, target.name
                            )
                            visible_since_owner_update = True
                            self.outbox_changed.set()
                            result = {
                                "ok": True,
                                "state": "queued",
                                "channel": target.name,
                                "messages": len(progress),
                            }
                elif call.name == "reply_expectation_close":
                    if reply_wait_turn or authority not in {"owner", "agent"}:
                        result = {"ok": False, "error": "tool_not_allowed"}
                    else:
                        draft.close_reply_expectation = True
                        result = {"ok": True, "state": "closed"}
                elif self.mcp.has_tool(call.name) or self.builtin_tools.has_tool(
                    call.name
                ):
                    if not call.id:
                        result = {"ok": False, "error": "missing_tool_call_id"}
                    else:
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
                context_plan, recalled = await self._prepare_owner_context(
                    current_events, turn_id
                )
                if authority == "owner":
                    tools = self._owner_tool_specs(context_plan, delivery_channel.name)
                messages.append(
                    self._owner_update_message(
                        updates, delivery_channel, context_plan, recalled
                    )
                )
                source_event_id = updates[-1].event_id
                force_response = False
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
                        "Use send_message for the last concrete failure reason, then "
                        "call respond to close the Turn.]"
                    ),
                }
            )
            force_response = True

    def _tool_source(self, name: str, *, allow_notify: bool) -> str:
        if name in {
            "respond",
            "send_message",
            "reply_expectation_close",
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
    ) -> dict[str, Any]:
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
                    while (
                        isinstance(result, str)
                        and len(result) > 1000
                        and estimated > self.config.max_input_tokens
                    ):
                        result = _truncate_tool_result_json(
                            result, max(1000, len(result) // 2)
                        )
                        block["content"] = result
                        estimated = size()
        log_event(
            logger,
            TRACE,
            "llm_context_fit",
            estimated_input=estimated,
            input_limit=self.config.max_input_tokens,
            history_dropped=dropped,
        )
        if estimated > self.config.max_input_tokens:
            log_event(
                logger,
                logging.WARNING,
                "llm_context_oversize",
                estimated_input=estimated,
                input_limit=self.config.max_input_tokens,
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
