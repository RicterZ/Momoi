import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...channel import Channel, ChannelMessage
from ...observability.context import log_context
from ...models import AgentReply, IncomingMessage, ProviderResponse, TurnDraft
from ...observability.events import TRACE, log_event
from ...observability.values import safe_preview
from ..turn_support import (
    tool_error_block,
    tool_result_block,
)
from ..tool_contracts.conversation import end_turn_correction, end_turn_tool_spec
from .harness import TurnHarness
from .protocol import assistant_history_message, parse_end_turn
from .runtime_tools import (
    begin_heartbeat, enable_tools, recall_owner_context, record_heartbeat_activity,
)
from .workflow import AgentWorkflow, TurnExecutionSpec


logger = logging.getLogger("momoi.runtime.turns")


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
    ended: bool
    reply: AgentReply | None


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
        ended = False
        reply = None

        request.messages.append(
            assistant_history_message(request.response.content, request.response.continuation)
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
                    call.name
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
                async def prepare_heartbeat_context(arguments):
                    prepared = await request.prepare_heartbeat_context(arguments)
                    request.draft.memory_context.update(prepared["memory_snapshots"])
                    return prepared

                result = await begin_heartbeat(
                    call,
                    heartbeat_turn=execution.heartbeat,
                    harness_started=request.harness.started,
                    enable_tool_groups=request.enable_tool_groups,
                    tools=request.tools,
                    tool_surface=self.tool_surface,
                    prepare_context=prepare_heartbeat_context,
                )
            elif call.name == "heartbeat_activity":
                result = record_heartbeat_activity(
                    call, heartbeat_turn=execution.heartbeat, draft=request.draft,
                    minimum_seconds=self.config.heartbeat.min_interval_seconds,
                    maximum_seconds=self.config.heartbeat.max_interval_seconds,
                )
            elif call.name == "goal_review":
                result = (
                    self.agenda_tools.finish_review(
                        execution.goal_id, call.arguments, request.draft,
                    )
                    if execution.goal_id else {"ok": False, "error": "tool_not_allowed"}
                )
            elif call.name == "recall":
                result = await recall_owner_context(
                    call,
                    current_events=request.current_events,
                    turn_id=request.turn_id,
                    submit_context=request.submit_owner_context,
                )
                if result.get("ok"):
                    record = self.store.context_plan(request.turn_id)
                    recalled = record.get("retrieval", {}).get("recall_memories", []) if record else []
                    request.draft.memory_context.update(self.store.memory_snapshots(
                        [item["id"] for item in recalled if isinstance(item.get("id"), int)]
                    ))
            elif call.name == "end_turn":
                fields = dict(
                    stage=execution.stage, turn_id=request.turn_id,
                    call_id=request.call_id, round=request.round_number,
                    channel=request.delivery_channel.name,
                )
                log_event(
                    logger, TRACE, "end_turn_received", **fields,
                    arguments=safe_preview(call.arguments, 1000),
                )
                if any(block["is_error"] for block in results):
                    result = {
                        "ok": False, "error": "end_turn_delivery_failed",
                        "message": (
                            "Handle the failed delivery before finishing. "
                            "Do not resend committed bubbles."
                        ),
                    }
                elif execution.goal_id:
                    result = {"ok": True, "state": "completed"}
                else:
                    reply, error = parse_end_turn(
                        call.arguments,
                        execution=execution,
                        visible_since_owner_update=visible,
                    )
                    result = (
                        {"ok": True, "state": "completed"}
                        if reply is not None else {"ok": False, "error": error}
                    )
                    if error:
                        schema = end_turn_tool_spec(execution.stage)["input_schema"]
                        result.update(end_turn_correction(error, schema, call.arguments))
                ended = bool(result.get("ok"))
                log_event(
                    logger, TRACE, "end_turn_accepted" if ended else "end_turn_rejected",
                    **fields,
                    **({"mood_decision": "updated" if reply and reply.mood_update else "unchanged"}
                       if ended else {"reason": result.get("error")}),
                )
            elif call.name in {"send_bubbles", "send_voice"}:
                dispatch = (
                    self.bubble_delivery.dispatch_voice
                    if call.name == "send_voice"
                    else self.bubble_delivery.dispatch
                )
                delivery = dispatch(
                    call,
                    turn_id=request.turn_id,
                    stage=execution.stage,
                    round_number=request.round_number,
                    delivery_channel=request.delivery_channel,
                    heartbeat_turn=execution.heartbeat,
                    reply_followup_turn=execution.reply_followup,
                    heartbeat_owner_event_revision=(
                        request.heartbeat_owner_event_revision
                    ),
                    previous_tool_name=previous_tool_name,
                    previous_bubbles=last_sent_bubbles,
                    previous_channel=last_sent_channel,
                )
                if call.name == "send_voice":
                    delivery = await delivery
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
                call.name
            ):
                result = self.agenda_tools.execute(
                    call,
                    request.draft,
                    authority=execution.authority,
                    source_event_id=request.source_event_id,
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
            if ended:
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
            ended=ended,
            reply=reply,
        )
