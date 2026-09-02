import copy
import json
import logging
import time
from typing import Any

from ...channel import (
    Channel,
    ChannelMessage,
)
from ...logging_context import TRACE, compact_log_value, log_context, log_event, new_trace_id, safe_preview
from ...models import AgentReply, IncomingMessage, TurnDraft
from ...storage import estimate_tokens
from . import (
    AgentWorkflow,
    TurnExecutionSpec,
    TurnHarness,
    WorkflowProtocolError,
)
from ..parsing import response_text
from .progress import (
    announce_field,
    apply_tool_announce,
    initial_announce_error_message,
    missing_initial_work_announce,
)
from .protocol import harness_correction, owner_request_messages, parse_end_turn
from ..protocol import (
    AUTONOMOUS_FINISH_SPEC,
)
from ..turn_support import (
    ExternalToolTurnError,
    MAX_CONSECUTIVE_TOOL_FAILURES,
    OwnerMessagesChanged,
    tool_error_block as _tool_error_block,
    tool_result_block as _tool_result_block,
)

logger = logging.getLogger("momoi.runtime.turns")
class AgentLoop:
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
        messages.append(
            self._owner_update_message(
                updates, delivery_channel, self.owner_context_baseline(current_events)
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
        execution: TurnExecutionSpec,
        source_event_id: str,
        turn_id: str,
        heartbeat_owner_event_revision: int | None = None,
        heartbeat_notification_key: str = "heartbeat.chat",
        delivery_channel: Channel,
        workflow: AgentWorkflow | None = None,
    ) -> AgentReply | dict[str, Any] | None:
        authority = execution.authority
        allow_notify = execution.allow_notify
        require_response = execution.require_response
        autonomous_goal_id = execution.goal_id
        heartbeat_turn = execution.heartbeat
        reply_wait_turn = execution.reply_followup
        accept_owner_updates = execution.accept_owner_updates
        dynamic_tool_policies = execution.dynamic_tool_policies
        allowed_capabilities = (
            set(execution.allowed_capabilities)
            if execution.allowed_capabilities is not None
            else None
        )
        artifact_root = execution.artifact_root
        external_tool_used = False
        force_autonomous_finish = False
        failed_tool_rounds = 0
        last_tool_error = ""
        history_messages = max(0, len(messages) - 1)
        visible_since_owner_update = False
        owner_work_acknowledged = False
        previous_tool_name: str | None = None
        last_sent_messages: list[ChannelMessage] | None = None
        last_sent_channel = ""
        llm_round = 0
        remind_owner_bubbles = False
        enable_tool_groups = (
            self.tool_surface.owner_enable_groups()
            if authority == "owner"
            else (
                self.tool_surface.self_directed_mcp_groups()
                if heartbeat_turn
                else {}
            )
        )
        stage = execution.stage
        if workflow is not None and workflow.stage != stage:
            raise ValueError("workflow and execution stages do not match")
        harness = TurnHarness.for_stage(stage)
        harness.validate_surface({str(tool["name"]) for tool in tools})
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
                previous_tool_name = None
                last_sent_messages = None
                last_sent_channel = ""
                tools, source_event_id = await self._append_owner_updates(
                    updates,
                    current_events,
                    turn_id,
                    authority,
                    tools,
                    messages,
                    delivery_channel,
                )
                harness.reset()
                force_autonomous_finish = False
                failed_tool_rounds = 0
                remind_owner_bubbles = False
            request_tools = (
                [AUTONOMOUS_FINISH_SPEC]
                if force_autonomous_finish
                else harness.project_surface(tools)
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
                history_messages = self.context_window.fit(
                    request_system,
                    messages,
                    request_tools,
                    history_messages,
                )
                request_messages = (
                    owner_request_messages(
                        messages,
                        remind_bubbles=remind_owner_bubbles and harness.started,
                    )
                    if authority == "owner"
                    else messages
                )
                self.context_window.check_budget(
                    turn_id, request_system, request_messages, request_tools
                )
            require_tool = bool(
                autonomous_goal_id or heartbeat_turn or reply_wait_turn or workflow
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
                            request_messages,
                            request_tools,
                            require_tool=require_tool,
                            current_events=current_events,
                            channel_name=delivery_channel.name,
                        )
                    else:
                        response = await self.provider.complete(
                            request_system,
                            request_messages,
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
                previous_tool_name = None
                last_sent_messages = None
                last_sent_channel = ""
                tools, source_event_id = await self._append_owner_updates(
                    updates,
                    current_events,
                    turn_id,
                    authority,
                    tools,
                    messages,
                    delivery_channel,
                )
                harness.reset()
                force_autonomous_finish = False
                failed_tool_rounds = 0
                remind_owner_bubbles = False
                continue
            except Exception as error:
                if external_tool_used:
                    raise ExternalToolTurnError(type(error).__name__) from error
                raise
            metrics = response.usage or {}
            remind_owner_bubbles = authority == "owner" and not any(
                call.name == "send_bubbles" for call in response.tool_calls
            )
            input_tokens = int(
                metrics.get(
                    "input",
                    estimate_tokens(
                        json.dumps(
                            {
                                "system": request_system,
                                "messages": request_messages,
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
                previous_tool_name = None
                last_sent_messages = None
                last_sent_channel = ""
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
                harness.reset()
                force_autonomous_finish = False
                failed_tool_rounds = 0
                remind_owner_bubbles = False
                continue
            if not response.tool_calls:
                if workflow is not None:
                    failed_tool_rounds += 1
                    if failed_tool_rounds >= MAX_CONSECUTIVE_TOOL_FAILURES:
                        raise WorkflowProtocolError(
                            last_tool_error or "repeated workflow protocol failures"
                        )
                    assistant_content = copy.deepcopy(response.content)
                    if response.reasoning:
                        assistant_content.insert(
                            0,
                            {"type": "reasoning", "text": response.reasoning},
                        )
                    messages.extend(
                        [
                            {"role": "assistant", "content": assistant_content},
                            {
                                "role": "user",
                                "content": workflow.no_tool_correction,
                            },
                        ]
                    )
                    continue
                if heartbeat_turn and not harness.started:
                    failed_tool_rounds += 1
                    if failed_tool_rounds >= MAX_CONSECUTIVE_TOOL_FAILURES:
                        raise ExternalToolTurnError("heartbeat_not_started")
                    messages.extend(
                        [
                            {"role": "assistant", "content": response.content},
                            {
                                "role": "user",
                                "content": (
                                    "[Trusted runtime protocol error. The previous "
                                    "text was not delivered. Call heartbeat_begin "
                                    "alone before any other Heartbeat action.]"
                                ),
                            },
                        ]
                    )
                    continue
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
                    reason="native_tool_call_required",
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": (
                                "[Trusted runtime protocol error. The previous text was "
                                "not delivered. Call recall first and alone as a native "
                                "tool call; never write or imitate tool syntax in text.]"
                                if authority == "owner" and not harness.started
                                else (
                                    "[Trusted runtime protocol error. The previous text "
                                    "was not delivered. Call send_bubbles with the "
                                    "owner-visible bubbles, without end_turn. After its "
                                    "result, call end_turn alone on the next step.]"
                                )
                            ),
                        },
                    ]
                )
                continue
            harness_error = harness.validate(
                response.tool_calls,
                has_assistant_text=bool(response_text(response.content)),
            )
            if harness_error is not None:
                failed_tool_rounds += 1
                if failed_tool_rounds >= MAX_CONSECUTIVE_TOOL_FAILURES:
                    error_type = (
                        WorkflowProtocolError
                        if workflow is not None
                        else (
                            ExternalToolTurnError if require_response else RuntimeError
                        )
                    )
                    raise error_type(harness_error)
                correction = harness_correction(
                    response.tool_calls,
                    harness_error,
                    require_response=require_response,
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": correction},
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
                reply, error = parse_end_turn(
                    response.tool_calls[0].arguments,
                    heartbeat_turn=heartbeat_turn,
                    owner_turn=authority == "owner",
                    reply_followup_turn=reply_wait_turn,
                    visible_since_owner_update=visible_since_owner_update,
                    heartbeat_min_interval_seconds=(
                        self.config.heartbeat.min_interval_seconds
                    ),
                    heartbeat_max_interval_seconds=(
                        self.config.heartbeat.max_interval_seconds
                    ),
                    validate_emotions=self.delivery_policy.validate_emotions,
                )
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
                            "content": [
                                _tool_error_block(response.tool_calls[0].id, error)
                            ],
                        },
                    ]
                )
                continue
            missing_announce = missing_initial_work_announce(
                response.tool_calls,
                request_tools,
                owner_work_acknowledged=owner_work_acknowledged,
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
                                "in this Turn. Use send_bubbles for the last concrete "
                                "failure reason without end_turn. After its result, call "
                                "end_turn alone on the next step.]"
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
                source = (
                    "workflow"
                    if workflow is not None and call.name in workflow.tool_names
                    else self.tool_executor.source(
                        call.name, allow_notify=allow_notify
                    )
                )
                journal_tool = source in {
                    "mcp",
                    "builtin",
                    "agenda",
                    "memory",
                    "workflow",
                }
                if journal_tool:
                    journal_arguments = dict(call.arguments)
                    journal_arguments.pop("say_to_owner", None)
                    self.tool_executor.journal(
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
                elif call.name == "heartbeat_begin":
                    requested = call.arguments.get("tool_groups")
                    if (
                        not heartbeat_turn
                        or harness.started
                        or not isinstance(requested, list)
                        or any(
                            not isinstance(group, str)
                            or group not in enable_tool_groups
                            for group in requested
                        )
                    ):
                        result = {
                            "ok": False,
                            "error": "invalid_heartbeat_begin",
                        }
                    else:
                        try:
                            prepared = await self.prepare_heartbeat_context(
                                call.arguments
                            )
                        except ValueError as error:
                            result = {
                                "ok": False,
                                "error": "invalid_heartbeat_begin",
                                "message": str(error),
                            }
                        else:
                            enabled_tools = self.tool_surface.append_visible(
                                tools,
                                [
                                    spec
                                    for group in dict.fromkeys(requested)
                                    for spec in enable_tool_groups[group]
                                ],
                            )
                            recalled = prepared["context"]
                            assert isinstance(recalled, dict)
                            result = {
                                "ok": True,
                                "state": "started",
                                "activity": call.arguments.get("activity"),
                                "mode": call.arguments.get("mode"),
                                "strategy": call.arguments.get("strategy"),
                                "memory": recalled["recall_memories"],
                                "status": recalled["query_recall"],
                                "reflection": recalled["reflection_memories"],
                                "episodes": recalled["episodes"],
                                "enabled_tools": enabled_tools,
                            }
                elif call.name == "recall":
                    try:
                        recalled = await self.submit_owner_context(
                            current_events, turn_id, call.arguments
                        )
                    except ValueError as error:
                        result = {
                            "ok": False,
                            "error": "invalid_recall",
                            "message": str(error),
                        }
                    else:
                        result = {
                            "ok": True,
                            "state": "recalled",
                            "memory": recalled["recall_memories"],
                            "status": recalled["query_recall"],
                            "reflection": recalled["reflection_memories"],
                            "episodes": recalled["episodes"],
                        }
                elif not harness.started and authority == "owner":
                    # The recall decision is what makes the rest of the Turn
                    # accountable, so it cannot be skipped by acting first.
                    result = {
                        "ok": False,
                        "error": "context_not_submitted",
                        "message": (
                            "Call recall first to decide what history "
                            "this input depends on."
                        ),
                    }
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
                elif call.name == "send_bubbles":
                    if autonomous_goal_id and allow_notify:
                        result = self.agenda_tools.execute(
                            call,
                            draft,
                            authority=authority,
                            source_event_id=source_event_id,
                            allow_notify=True,
                        )
                    else:
                        delivery = self.bubble_delivery.dispatch(
                            call,
                            turn_id=turn_id,
                            stage=stage,
                            round_number=llm_round,
                            delivery_channel=delivery_channel,
                            response_required=require_response,
                            heartbeat_turn=heartbeat_turn,
                            reply_followup_turn=reply_wait_turn,
                            heartbeat_owner_event_revision=(
                                heartbeat_owner_event_revision
                            ),
                            heartbeat_notification_key=heartbeat_notification_key,
                            previous_tool_name=previous_tool_name,
                            previous_bubbles=last_sent_messages,
                            previous_channel=last_sent_channel,
                        )
                        result = delivery.result
                        if delivery.bubbles is not None:
                            visible_since_owner_update = True
                            owner_work_acknowledged = (
                                owner_work_acknowledged
                                or delivery.acknowledges_work
                            )
                            last_sent_messages = copy.deepcopy(delivery.bubbles)
                            last_sent_channel = delivery.channel
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
                        enabled_tools = self.tool_surface.append_visible(
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
                elif call.name == "read_tool_result":
                    result = self.tool_results.read(
                        call.arguments.get("result_ref"),
                        call.arguments.get("cursor"),
                        max_chars=self.config.tool_result_max_chars,
                        provenance={
                            "source": "runtime",
                            "tool": "read_tool_result",
                        },
                    )
                elif workflow is not None and call.name in workflow.tool_names:
                    result = await workflow.execute_tool(call)
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
                            text = apply_tool_announce(
                                call.arguments,
                                announce,
                            )
                            if announce_delivered_in_batch:
                                text = None
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
                            result, has_external_effect = (
                                await self.tool_executor.execute_external(
                                    call,
                                    source,
                                    turn_id=turn_id,
                                    allowed_capabilities=allowed_capabilities,
                                    artifact_root=artifact_root,
                                )
                            )
                        external_tool_used = (
                            external_tool_used or has_external_effect
                        )
                elif self.agenda_tools.has_tool(call.name, allow_notify=allow_notify):
                    result = self.agenda_tools.execute(
                        call,
                        draft,
                        authority=authority,
                        source_event_id=source_event_id,
                        allow_notify=allow_notify,
                    )
                else:
                    result = await self.memory_tools.execute_async(
                        call, current_events, draft
                    )
                if "provenance" not in result:
                    result = self.tool_executor.normalize(call, result, source)
                if result.get("ok"):
                    harness.accept(call.name)
                    last_tool_error = ""
                else:
                    last_tool_error = str(
                        result.get("message") or result.get("error") or "tool_failed"
                    )
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
                    self.tool_executor.journal(
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
                previous_tool_name = call.name
                if workflow is not None and workflow.is_complete():
                    results.extend(
                        _tool_error_block(
                            pending.id, "workflow_already_completed"
                        )
                        for pending in response.tool_calls[index + 1 :]
                    )
                    break
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
                previous_tool_name = None
                last_sent_messages = None
                last_sent_channel = ""
                tools, source_event_id = await self._append_owner_updates(
                    updates,
                    current_events,
                    turn_id,
                    authority,
                    tools,
                    messages,
                    delivery_channel,
                )
                harness.reset()
                failed_tool_rounds = 0
                continue
            if workflow is not None and workflow.is_complete():
                return workflow.completion_result() or {"ok": True}
            if any(not block["is_error"] for block in results):
                failed_tool_rounds = 0
                continue
            failed_tool_rounds += 1
            if failed_tool_rounds < MAX_CONSECUTIVE_TOOL_FAILURES:
                continue
            if not require_response:
                error_type = WorkflowProtocolError if workflow else RuntimeError
                raise error_type(last_tool_error or "repeated tool validation failures")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[Trusted runtime protocol stop. Tool calls failed validation "
                        "three consecutive times. Do not retry tools in this Turn. "
                        "Use send_bubbles for the last concrete failure reason without "
                        "end_turn. After its result, call end_turn alone on the next "
                        "step.]"
                    ),
                }
            )

    async def _run_agent_workflow(
        self,
        system: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        turn_id: str,
        workflow: AgentWorkflow,
    ) -> dict[str, Any]:
        result = await self._run_tool_loop(
            system,
            messages,
            tools,
            [],
            TurnDraft(),
            execution=TurnExecutionSpec(workflow.stage),
            source_event_id=turn_id,
            turn_id=turn_id,
            delivery_channel=self.channel,
            workflow=workflow,
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"{workflow.stage} ended without workflow state")
        return result
