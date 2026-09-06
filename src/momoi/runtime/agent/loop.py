import logging
from typing import Any

from ...channel import (
    Channel,
)
from ...observability.events import TRACE, log_event
from ...observability.values import safe_preview
from ...models import AgentReply, IncomingMessage, TurnDraft
from . import (
    AgentWorkflow,
    TurnExecutionSpec,
    TurnHarness,
    WorkflowProtocolError,
)
from .protocol import (
    handle_no_tool_response,
    parse_end_turn,
)
from .tool_batch import ToolBatchRequest, ToolBatchState
from ..turn_support import (
    ExternalToolTurnError,
    MAX_CONSECUTIVE_TOOL_FAILURES,
    OwnerMessagesChanged,
    tool_error_block as _tool_error_block,
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
        delivery_channel: Channel,
        workflow: AgentWorkflow | None = None,
    ) -> AgentReply | dict[str, Any] | None:
        authority = execution.authority
        require_response = execution.require_response
        autonomous_goal_id = execution.goal_id
        heartbeat_turn = execution.heartbeat
        reply_wait_turn = execution.reply_followup
        accept_owner_updates = execution.accept_owner_updates
        dynamic_tool_policies = execution.dynamic_tool_policies
        external_tool_used = False
        failed_tool_rounds = 0
        last_tool_error = ""
        history_messages = max(0, len(messages) - 1)
        visible_since_owner_update = False
        previous_tool_name: str | None = None
        last_sent_messages = None
        last_sent_channel = ""
        llm_round = 0
        remind_owner_bubbles = False
        enable_tool_groups = self.tool_surface.mcp_server_groups()
        stage = execution.stage
        permitted_tools = execution.permitted_tools
        voice_allowed = (
            self.bubble_delivery.tts_provider is not None
            and callable(getattr(delivery_channel, "send_voice", None))
        )
        if workflow is not None and workflow.stage != stage:
            raise ValueError("workflow and execution stages do not match")
        harness = TurnHarness.for_stage(
            stage,
            progress_tool_names=(
                self.tool_surface.owner_progress_tool_names()
                if stage == "owner"
                else frozenset()
            ),
            permitted_tool_names=permitted_tools,
            blocked_tool_names=frozenset() if voice_allowed else frozenset({"send_voice"}),
        )
        harness.validate_surface({str(tool["name"]) for tool in tools})
        while True:
            if reply_wait_turn and self.store.pending_owner_reply() is None:
                return None
            updates = (
                await self.owner_updates.settle(
                    current_events, delivery_channel.name
                )
                if accept_owner_updates
                else []
            )
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
                remind_owner_bubbles = False
            required_tool = harness.spec.first_tool if not harness.started else None
            if (required_tool == "send_bubbles" and voice_allowed
                    and (permitted_tools is None or "send_voice" in permitted_tools)):
                # The harness accepts either delivery form for the opening reply.
                required_tool = None
            request_tools = tools
            llm_round += 1
            require_tool = bool(
                autonomous_goal_id or heartbeat_turn or reply_wait_turn or workflow
            ) or (
                require_response and self.provider.config.api_format == "openai"
            )

            async def complete(
                request_system: list[dict[str, Any]],
                request_messages: list[dict[str, Any]],
                projected_tools: list[dict[str, Any]],
                required: bool,
                selected_tool: str | None,
            ):
                if accept_owner_updates:
                    return await self.owner_updates.complete(
                        request_system,
                        request_messages,
                        projected_tools,
                        require_tool=required,
                        required_tool=selected_tool,
                        current_events=current_events,
                        channel_name=delivery_channel.name,
                        provider=self.provider,
                    )
                return await self.provider.complete(
                    request_system,
                    request_messages,
                    projected_tools,
                    require_tool=required,
                    required_tool=selected_tool,
                )

            try:
                model_round = await self.model_round.run(
                    system,
                    messages,
                    request_tools,
                    complete=complete,
                    system_policy=(
                        self._system_with_tool_policies
                        if dynamic_tool_policies
                        else None
                    ),
                    authority=authority,
                    remind_owner_bubbles=remind_owner_bubbles,
                    harness_started=harness.started,
                    require_tool=require_tool,
                    required_tool=required_tool,
                    history_messages=history_messages,
                    stage=stage,
                    turn_id=turn_id,
                    channel=delivery_channel.name,
                    goal_id=autonomous_goal_id,
                    round_number=llm_round,
                )
            except OwnerMessagesChanged as interruption:
                updates = list(interruption.updates)
                updates.extend(
                    await self.owner_updates.settle(
                        current_events, delivery_channel.name
                    )
                )
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
                remind_owner_bubbles = False
                continue
            except Exception as error:
                if external_tool_used:
                    raise ExternalToolTurnError(type(error).__name__) from error
                raise
            response = model_round.response
            request_tools = model_round.request_tools
            call_id = model_round.call_id
            history_messages = model_round.history_messages
            remind_owner_bubbles = model_round.remind_owner_bubbles
            updates = (
                await self.owner_updates.settle(
                    current_events, delivery_channel.name
                )
                if accept_owner_updates
                else []
            )
            if updates:
                visible_since_owner_update = False
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
                failed_tool_rounds = 0
                remind_owner_bubbles = False
                continue
            if not response.tool_calls:
                resolution = handle_no_tool_response(
                    messages,
                    response.content,
                    workflow_correction=(
                        workflow.no_tool_correction if workflow is not None else None
                    ),
                    heartbeat_turn=heartbeat_turn,
                    harness_started=harness.started,
                    goal_turn=autonomous_goal_id is not None,
                    require_response=require_response,
                    owner_turn=authority == "owner",
                    failed_rounds=failed_tool_rounds,
                    last_tool_error=last_tool_error,
                    external_effect=external_tool_used,
                )
                failed_tool_rounds = resolution.failed_rounds
                if resolution.log_rejection:
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
                if resolution.action == "return":
                    return None
                continue
            harness_error = harness.validate(
                response.tool_calls,
                required_tool=required_tool,
            )
            if harness_error is not None:
                failed_tool_rounds += 1
                log_event(
                    logger,
                    logging.DEBUG,
                    "llm_protocol_rejected",
                    stage=stage,
                    turn_id=turn_id,
                    call_id=call_id,
                    round=llm_round,
                    channel=delivery_channel.name,
                    reason=harness_error,
                    tool_names=[call.name for call in response.tool_calls],
                    consecutive_failures=failed_tool_rounds,
                    failure_limit=MAX_CONSECUTIVE_TOOL_FAILURES,
                )
                if failed_tool_rounds >= MAX_CONSECUTIVE_TOOL_FAILURES:
                    error_type = (
                        WorkflowProtocolError
                        if workflow is not None
                        else (
                            ExternalToolTurnError
                            if external_tool_used
                            else WorkflowProtocolError
                        )
                    )
                    raise error_type(harness_error)
                correction = [
                    _tool_error_block(call.id, harness_error)
                    for call in response.tool_calls
                ]
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": correction},
                    ]
                )
                continue
            harness.observe_calls(response.tool_calls)
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
                    execution=execution,
                    visible_since_owner_update=visible_since_owner_update,
                    heartbeat_min_interval_seconds=(
                        self.config.heartbeat.min_interval_seconds
                    ),
                    heartbeat_max_interval_seconds=(
                        self.config.heartbeat.max_interval_seconds
                    ),
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
            batch = await self.tool_batch.execute(
                ToolBatchRequest(
                    response=response,
                    messages=messages,
                    request_tools=request_tools,
                    tools=tools,
                    enable_tool_groups=enable_tool_groups,
                    current_events=current_events,
                    draft=draft,
                    harness=harness,
                    execution=execution,
                    source_event_id=source_event_id,
                    turn_id=turn_id,
                    call_id=call_id,
                    round_number=llm_round,
                    delivery_channel=delivery_channel,
                    heartbeat_owner_event_revision=heartbeat_owner_event_revision,
                    workflow=workflow,
                    state=ToolBatchState(
                        visible_since_owner_update=visible_since_owner_update,
                        previous_tool_name=previous_tool_name,
                        last_sent_bubbles=last_sent_messages,
                        last_sent_channel=last_sent_channel,
                    ),
                    prepare_heartbeat_context=self.prepare_heartbeat_context,
                    submit_owner_context=self.submit_owner_context,
                    settle_owner_updates=self.owner_updates.settle,
                )
            )
            results = batch.results
            updates = batch.owner_updates
            external_tool_used = external_tool_used or batch.external_effect
            visible_since_owner_update = batch.state.visible_since_owner_update
            previous_tool_name = batch.state.previous_tool_name
            last_sent_messages = batch.state.last_sent_bubbles
            last_sent_channel = batch.state.last_sent_channel
            last_tool_error = batch.last_tool_error
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
            if (
                autonomous_goal_id
                and len(response.tool_calls) == 1
                and response.tool_calls[0].name == "end_turn"
                and results and not results[0]["is_error"]
            ):
                return None
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
