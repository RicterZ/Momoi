import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...channel import Channel, ChannelMessage
from ...observability.context import log_context
from ...models import IncomingMessage, ProviderResponse, TurnDraft
from ..turn_support import (
    tool_error_block,
    tool_result_block,
)
from .harness import TurnHarness
from .runtime_tools import begin_heartbeat, enable_tools, recall_owner_context
from .workflow import AgentWorkflow, TurnExecutionSpec


SettleOwnerUpdates = Callable[
    [list[IncomingMessage], str], Awaitable[list[IncomingMessage]]
]
PrepareHeartbeatContext = Callable[[dict[str, Any]], Awaitable[dict[str, object]]]
SubmitOwnerContext = Callable[
    [list[IncomingMessage], str, dict[str, Any]],
    Awaitable[dict[str, object]],
]


@dataclass(frozen=True)
class ToolBatchState:
    visible_since_owner_update: bool = False
    previous_tool_name: str | None = None
    last_sent_bubbles: list[ChannelMessage] | None = None
    last_sent_channel: str = ""


@dataclass(frozen=True)
class ToolBatchRequest:
    response: ProviderResponse
    messages: list[dict[str, Any]]
    request_tools: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    enable_tool_groups: dict[str, list[dict[str, Any]]]
    current_events: list[IncomingMessage]
    draft: TurnDraft
    harness: TurnHarness
    execution: TurnExecutionSpec
    source_event_id: str
    turn_id: str
    call_id: str
    round_number: int
    delivery_channel: Channel
    heartbeat_owner_event_revision: int | None
    heartbeat_notification_key: str
    workflow: AgentWorkflow | None
    state: ToolBatchState
    prepare_heartbeat_context: PrepareHeartbeatContext
    submit_owner_context: SubmitOwnerContext
    settle_owner_updates: SettleOwnerUpdates


@dataclass(frozen=True)
class ToolBatchResult:
    results: list[dict[str, Any]]
    owner_updates: list[IncomingMessage]
    external_effect: bool
    state: ToolBatchState
    last_tool_error: str


class ToolBatchExecutor:
    """Execute one validated model tool-call batch in response order."""

    def __init__(
        self,
        config: Any,
        store: Any,
        tool_surface: Any,
        tool_executor: Any,
        bubble_delivery: Any,
        agenda_tools: Any,
        memory_tools: Any,
        thinking_tools: Any,
        tool_results: Any,
        outbox_changed: Any,
    ) -> None:
        self.config = config
        self.store = store
        self.tool_surface = tool_surface
        self.tool_executor = tool_executor
        self.bubble_delivery = bubble_delivery
        self.agenda_tools = agenda_tools
        self.memory_tools = memory_tools
        self.thinking_tools = thinking_tools
        self.tool_results = tool_results
        self.outbox_changed = outbox_changed

    async def execute(self, request: ToolBatchRequest) -> ToolBatchResult:
        execution = request.execution
        state = request.state
        visible = state.visible_since_owner_update
        previous_tool_name = state.previous_tool_name
        last_sent_bubbles = state.last_sent_bubbles
        last_sent_channel = state.last_sent_channel
        last_tool_error = ""
        external_effect = False

        # Tool execution strips harness-only arguments in place. Keep an
        # independent history copy containing exactly what the owner heard.
        assistant_history_content = copy.deepcopy(request.response.content)
        if request.response.reasoning and request.response.tool_calls:
            assistant_history_content.insert(
                0,
                {"type": "reasoning", "text": request.response.reasoning},
            )
        request.messages.append(
            {"role": "assistant", "content": assistant_history_content}
        )
        results: list[dict[str, Any]] = []
        owner_updates: list[IncomingMessage] = []
        allowed_tool_names = {str(spec["name"]) for spec in request.request_tools}

        for index, call in enumerate(request.response.tool_calls):
            source = (
                "workflow"
                if request.workflow is not None
                and call.name in request.workflow.tool_names
                else self.tool_executor.source(
                    call.name, allow_notify=execution.allow_notify
                )
            )
            trace = self.tool_executor.begin_trace(
                call,
                source,
                turn_id=request.turn_id,
                stage=execution.stage,
                call_id=request.call_id,
                round_number=request.round_number,
                channel=request.delivery_channel.name,
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
                result = await begin_heartbeat(
                    call,
                    heartbeat_turn=execution.heartbeat,
                    harness_started=request.harness.started,
                    enable_tool_groups=request.enable_tool_groups,
                    tools=request.tools,
                    tool_surface=self.tool_surface,
                    prepare_context=request.prepare_heartbeat_context,
                )
            elif call.name == "recall":
                result = await recall_owner_context(
                    call,
                    current_events=request.current_events,
                    turn_id=request.turn_id,
                    submit_context=request.submit_owner_context,
                )
            elif not request.harness.started and execution.authority == "owner":
                result = {
                    "ok": False,
                    "error": "context_not_submitted",
                    "message": (
                        "Call recall first to decide what history this input depends on."
                    ),
                }
            elif call.name == "end_turn":
                result = {
                    "ok": False,
                    "error": (
                        "end_turn_must_be_the_only_terminal_tool"
                        if execution.require_response
                        else "tool_not_allowed"
                    ),
                }
            elif call.name == "autonomous_finish":
                result = {
                    "ok": False,
                    "error": "autonomous_finish_must_be_the_only_terminal_tool",
                }
            elif call.name == "send_bubbles":
                if execution.goal_id and execution.allow_notify:
                    result = self.agenda_tools.execute(
                        call,
                        request.draft,
                        authority=execution.authority,
                        source_event_id=request.source_event_id,
                        allow_notify=True,
                    )
                else:
                    delivery = self.bubble_delivery.dispatch(
                        call,
                        turn_id=request.turn_id,
                        stage=execution.stage,
                        round_number=request.round_number,
                        delivery_channel=request.delivery_channel,
                        response_required=execution.require_response,
                        heartbeat_turn=execution.heartbeat,
                        reply_followup_turn=execution.reply_followup,
                        heartbeat_owner_event_revision=(
                            request.heartbeat_owner_event_revision
                        ),
                        heartbeat_notification_key=request.heartbeat_notification_key,
                        previous_tool_name=previous_tool_name,
                        previous_bubbles=last_sent_bubbles,
                        previous_channel=last_sent_channel,
                    )
                    result = delivery.result
                    if delivery.bubbles is not None:
                        visible = True
                        last_sent_bubbles = copy.deepcopy(delivery.bubbles)
                        last_sent_channel = delivery.channel
            elif call.name == "tool_enable":
                result = enable_tools(
                    call,
                    enable_tool_groups=request.enable_tool_groups,
                    tools=request.tools,
                    tool_surface=self.tool_surface,
                )
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
            elif (
                request.workflow is not None
                and call.name in request.workflow.tool_names
            ):
                result = await request.workflow.execute_tool(call)
            elif self.tool_executor.is_external(call.name):
                result = None
                if not call.id:
                    result = {"ok": False, "error": "missing_tool_call_id"}
                if result is None:
                    with log_context(
                        stage=execution.stage,
                        turn_id=request.turn_id,
                        call_id=request.call_id,
                        round=request.round_number,
                        channel=request.delivery_channel.name,
                        goal_id=execution.goal_id,
                        tool_call_id=call.id,
                        tool_name=call.name,
                    ):
                        result, call_has_external_effect = (
                            await self.tool_executor.execute_external(
                                call,
                                source,
                                turn_id=request.turn_id,
                                allowed_capabilities=(
                                    set(execution.allowed_capabilities)
                                    if execution.allowed_capabilities is not None
                                    else None
                                ),
                                artifact_root=(
                                    Path(execution.artifact_root)
                                    if execution.artifact_root is not None
                                    else None
                                ),
                            )
                        )
                    external_effect = external_effect or call_has_external_effect
            elif self.agenda_tools.has_tool(
                call.name, allow_notify=execution.allow_notify
            ):
                result = self.agenda_tools.execute(
                    call,
                    request.draft,
                    authority=execution.authority,
                    source_event_id=request.source_event_id,
                    allow_notify=execution.allow_notify,
                )
            elif source == "memory":
                result = await self.memory_tools.execute_async(
                    call, request.current_events, request.draft
                )
            elif source == "thinking":
                result = self.thinking_tools.execute(call)
            else:
                result = {"ok": False, "error": "tool_not_allowed"}

            if "provenance" not in result:
                result = self.tool_executor.normalize(call, result, source)
            if result.get("ok"):
                request.harness.accept(call.name)
                last_tool_error = ""
            else:
                last_tool_error = str(
                    result.get("message") or result.get("error") or "tool_failed"
                )
            self.tool_executor.finish_trace(trace, call, result, request.draft)
            results.append(tool_result_block(call.id, result))
            previous_tool_name = call.name

            if request.workflow is not None and request.workflow.is_complete():
                results.extend(
                    tool_error_block(pending.id, "workflow_already_completed")
                    for pending in request.response.tool_calls[index + 1 :]
                )
                break
            if execution.accept_owner_updates:
                owner_updates = await request.settle_owner_updates(
                    request.current_events, request.delivery_channel.name
                )
                if owner_updates:
                    results.extend(
                        tool_error_block(pending.id, "superseded_by_owner_update")
                        for pending in request.response.tool_calls[index + 1 :]
                    )
                    break

        request.messages.append({"role": "user", "content": results})
        return ToolBatchResult(
            results=results,
            owner_updates=owner_updates,
            external_effect=external_effect,
            state=ToolBatchState(
                visible_since_owner_update=visible,
                previous_tool_name=previous_tool_name,
                last_sent_bubbles=last_sent_bubbles,
                last_sent_channel=last_sent_channel,
            ),
            last_tool_error=last_tool_error,
        )
