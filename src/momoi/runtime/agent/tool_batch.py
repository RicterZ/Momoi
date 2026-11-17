from ..context.triggers import attach_reply_episode_triggers
import time
import asyncio
import copy
from dataclasses import replace
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...channel import Channel, ChannelMessage
from ...storage.core.timestamps import context_timestamp
from ...observability.context import log_context
from ...models import MessageRecalled, IncomingMessage, ProviderResponse, ToolCall, TurnDraft
from ...mcp.content import attachment_blocks
from ..turn_support import (
    tool_error_block,
    tool_result_block,
)
from ...tools.validation import validate_tool_arguments
from .harness import TurnHarness
from .protocol import assistant_history_message
from .runtime_tools import (
    begin_heartbeat, search_tools, enable_tools, recall_owner_context, record_heartbeat_activity,
)
from .workflow import ASSISTANT_EXCHANGE_STAGES, AgentWorkflow, TurnExecutionSpec
from ..tool_contracts.subagent import PLANNER_SUBAGENT_TOOL_NAMES


logger = logging.getLogger("momoi.runtime.turns")
PARALLEL_HISTORY_READS = frozenset({
    "episode_search", "episode_read", "memory_search", "thinking_search", "thinking_read",
})


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
    previous_tool_name: str | None = None
    last_sent_bubbles: list[ChannelMessage] | None = None
    last_sent_channel: str = ""
    calls_since_reply: int = 0


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
    wait_owner_messages: Any = None
    system: Any = None
    context_messages: Any = None
    pause_requested: Any = None


@dataclass(frozen=True)
class ToolBatchResult:
    results: list[dict[str, Any]]
    owner_updates: list[IncomingMessage]
    external_effect: bool
    state: ToolBatchState
    last_tool_error: str
    protocol_error: bool = False
    updates_in_result: bool = False


@dataclass
class _BatchEffects:
    # Parallel calls must observe shared updates even after awaiting a reply.
    previous_tool_name: str | None = None
    last_sent_bubbles: list[ChannelMessage] | None = None
    last_sent_channel: str = ""
    owner_updates: list[IncomingMessage] = field(default_factory=list)
    external_effect: bool = False
    updates_in_result: bool = False


def _tool_log_context(
    request: ToolBatchRequest, call: ToolCall,
) -> AbstractContextManager[dict[str, Any]]:
    return log_context(
        stage=request.execution.stage,
        turn_id=request.turn_id,
        call_id=request.call_id,
        round=request.round_number,
        channel=request.delivery_channel.name,
        goal_id=request.execution.goal_id,
        tool_call_id=call.id,
        tool_name=call.name,
    )


def subagent_view(task: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    view = {"task_id": task["id"], "title": task["title"], "status": task["status"]}
    if task.get("queue_position") is not None:
        view["queue_position"] = task["queue_position"]
    if task["reports"]:
        view["latest_report"] = task["reports"][-1]["progress"]
    if task.get("summary"):
        view["summary"] = task["summary"]
    if detail:
        view.update(task=task["task"], reports=[item["progress"] for item in task["reports"]],
                    output_refs=task["output_refs"])
    return view


class ToolBatchExecutor:
    """Overlap independent reads and one reply; keep state-changing tools ordered."""

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
        replyer: Any = None,
        *, memory: Any, provider: Any = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self.store = store
        self.tool_surface = tool_surface
        self.tool_executor = tool_executor
        self.bubble_delivery = bubble_delivery
        self.agenda_tools = agenda_tools
        self.memory_tools = memory_tools
        self.memory = memory
        self.thinking_tools = thinking_tools
        self.tool_results = tool_results
        self.outbox_changed = outbox_changed
        self.replyer = replyer
        self.subagent_changed: Callable[[], None] = lambda: None
        self.subagent_cancelled: Callable[[str], None] = lambda task_id: None

    def _subagent_tool(self, call: ToolCall, request: "ToolBatchRequest") -> dict[str, Any]:
        channel = request.delivery_channel.name
        try:
            if call.name == "create_subagent":
                task = self.store.create_subagent_task(
                    request.turn_id, channel, call.arguments["title"], call.arguments["task"],
                )
                self.subagent_changed()
                return {"ok": True, **subagent_view(task), "message": (
                    "已派发，Worker 正在执行。" if not task.get("queue_position")
                    else f"已排队，前面还有 {task['queue_position']} 个任务。"
                ) + "Worker 汇报时会唤醒你。"}
            if call.name == "subagent_cancel":
                task = self.store.cancel_subagent_task(call.arguments["task_id"], channel)
                self.subagent_cancelled(task["id"])
                self.subagent_changed()
                return {"ok": True, **subagent_view(task)}
            task_id = call.arguments.get("task_id")
            if task_id:
                task = self.store.subagent_task(task_id)
                if task is None or task["channel"] != channel:
                    raise ValueError("task not found in this channel")
                return {"ok": True, **subagent_view(task, detail=True)}
            return {"ok": True, "tasks": [subagent_view(task) for task in self.store.subagent_tasks(channel)]}
        except (ValueError, KeyError, TypeError) as error:
            return {"ok": False, "error": "invalid_subagent_arguments", "message": str(error)}

    async def _wait(
        self, call: ToolCall, request: ToolBatchRequest, batch: _BatchEffects,
        image_blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        execution = request.execution
        if execution.stage != "owner" or request.wait_owner_messages is None:
            result = {"ok": False, "error": "tool_not_allowed"}
        else:
            batch.owner_updates = await request.wait_owner_messages(
                call.arguments['seconds'], request.current_events, request.delivery_channel.name
            )
            batch.updates_in_result = True
            received = []
            for event in batch.owner_updates:
                item = {"time": context_timestamp(event.received_at, self.store.timezone),
                        "message_id": event.message_id, "text": event.text}
                notice = event.delivery_context.get("channel_notice")
                if notice:
                    item["event"] = notice
                    blocks = []
                else:
                    blocks = request.delivery_channel.content_blocks(event.segments)
                descriptions = [block['text'] for block in blocks if block.get('type') == 'text']
                if descriptions:
                    item['attachments'] = descriptions
                if any(block.get('type') == 'image' for block in blocks):
                    item['attachment_ref'] = event.event_id
                    # Binary inputs stay outside JSON; each group identifies its message.
                    image_blocks.extend([
                        {'type': 'text', 'text': f'[wait result {call.id} attachment_ref={event.event_id}]'},
                        *blocks,
                    ])
                received.append(item)
            result = {"ok": True, "waited_seconds": call.arguments['seconds'],
                      "received_messages": len(received),
                      "message": "等待结束，以下是期间收到的用户消息。" if received else "等待结束，未收到新消息。",
                      "messages": received}
        return result

    async def _recall(
        self, call: ToolCall, request: ToolBatchRequest,
    ) -> dict[str, Any]:
        execution = request.execution
        # Recall can issue its own topic-selection model request.  Preserve
        # the enclosing Turn so that its CUES reasoning is part of this
        # timeline rather than an orphaned thinking record.
        with _tool_log_context(request, call):
            if execution.heartbeat:
                try:
                    from ..tool_contracts.context import recall_search_arguments
                    search = recall_search_arguments(call.arguments)
                    prepared = await request.prepare_heartbeat_context({
                        "activity": " / ".join(search["semantic"] or search["keyword"]) or "记忆筛选",
                        "mode": "work",
                        "recall_mode": "search",
                        "recall_queries": [{"semantic": value, "keywords": search["keyword"]}
                                           for value in (search["semantic"] or [""])],
                        "memory_filters": search["filters"],
                        "episode_time_range": search["time_range"],
                        "strategy": ["Check the candidate message against prior discussion."],
                    })
                    request.draft.memory_context.update(prepared["memory_snapshots"])
                    context = prepared["context"]
                    result = {
                        "ok": True, "state": "recalled",
                        "memory": context.get("memory_records", []),
                        "reflection": context.get("reflection_records", []),
                        **({"reflection_note": "复盘记忆可能过时，仅作辅助；以当前证据为准。"} if context.get("reflection_records") else {}),
                        "episodes": context.get("episode_records", []),
                    }
                except (KeyError, TypeError, ValueError) as error:
                    result = {"ok": False, "error": "invalid_recall", "message": str(error)}
            else:
                result = await recall_owner_context(
                    call,
                    current_events=request.current_events,
                    turn_id=request.turn_id,
                    submit_context=request.submit_owner_context,
                )
        if result.get("ok") and not execution.heartbeat:
            record = self.store.context_plan(request.turn_id)
            recalled = record.get("retrieval", {}).get("recall_memories", []) if record else []
            request.draft.memory_context.update(self.memory.snapshots(
                [item["id"] for item in recalled if isinstance(item.get("id"), int)]
            ))
        if result.get("ok"):
            request.draft.record_memory_access(
                "recalled", [item["id"] for item in result.get("memory", [])
                             if isinstance(item.get("id"), int)], event_key=call.id,
            )
        return result

    async def _reply(
        self, call: ToolCall, request: ToolBatchRequest, batch: _BatchEffects,
        *, first_call_reply: bool,
    ) -> dict[str, Any]:
        execution = request.execution
        target = self.bubble_delivery.channels.get(
            call.arguments.get("channel") or request.delivery_channel.name, request.delivery_channel)
        cached_reply = self.store.committed_reply(request.turn_id, call.id)
        if cached_reply is not None:
            result = await self.bubble_delivery.wait_reply(request.turn_id, call.id)
            if result.get("ok"):
                batch.last_sent_bubbles = copy.deepcopy(result["bubbles"])
                batch.last_sent_channel = str(result["channel"])
        elif getattr(target, "required_reply_mode", None) and (
            call.arguments.get("mode", "text") != target.required_reply_mode or call.arguments.get("attachments")
        ):
            result = {"ok": False, "error": "call_requires_voice", "message": "当前目标为 QQ 语音电话；请使用 reply(mode=voice)，附件请另发普通 QQ。"}
        elif call.arguments.get("mode", "text") == "voice" and (
            self.bubble_delivery.tts_provider is None
            or not callable(getattr(target, "send_voice", None))
        ):
            result = {"ok": False, "error": "voice_not_supported",
                      "message": "当前渠道无法发送语音，请使用 reply(mode=text)。"}
        elif self.replyer is None:
            result = {"ok": False, "error": "replyer_unavailable"}
        else:
            try:
                reply_request = replace(request, delivery_channel=target,
                                        state=ToolBatchState(batch.previous_tool_name, batch.last_sent_bubbles, batch.last_sent_channel))
                owner_revision = self.store.owner_channel_revision(target.name)
                if target.name == "dashboard":
                    await target.set_typing(True)
                try:
                    bubbles = await self.replyer.generate(call, reply_request)
                finally:
                    if target.name == "dashboard":
                        await target.set_typing(False)
                mode = call.arguments.get("mode", "text")
                first_call_updated = first_call_reply and (
                    bool(batch.owner_updates) or self.store.owner_channel_revision(target.name) != owner_revision
                )
                if first_call_updated:
                    # Give the first response a chance; reconsider the rest after the interjection.
                    bubbles = bubbles[:1]
                if mode == "text":
                    bubbles = [*bubbles, *copy.deepcopy(call.arguments.get("attachments", []))]
                if not bubbles:
                    result = {"ok": True, "state": "no_reply", "bubbles": [], "mode": mode}
                else:
                    delivery_call = ToolCall(call.id, "send_voice" if mode == "voice" else "send_bubbles",
                                             {"text": "\n\n".join(bubbles)} if mode == "voice" else {"bubbles": bubbles})
                    dispatch = self.bubble_delivery.dispatch_voice if mode == "voice" else self.bubble_delivery.dispatch
                    call_context = next((event.delivery_context for event in reversed(request.current_events)
                        if event.channel == target.name and event.delivery_context
                        and not event.delivery_context.get("channel_notice")), {})
                    if first_call_updated and target.session_valid(call_context):
                        call_context = {**call_context, **target.routing_context()}
                    delivery = dispatch(delivery_call, turn_id=request.turn_id, stage=execution.stage,
                        round_number=request.round_number, delivery_channel=target,
                        heartbeat_turn=execution.heartbeat,
                        heartbeat_owner_event_revision=request.heartbeat_owner_event_revision,
                        current_events=request.current_events,
                        previous_tool_name=batch.previous_tool_name, previous_bubbles=batch.last_sent_bubbles,
                        previous_channel=batch.last_sent_channel,
                        **({"delivery_context": call_context} if mode == "voice" and call_context else {}))
                    if mode == "voice":
                        delivery = await delivery
                    if delivery.result.get("ok"):
                        if ((not first_call_reply and self.store.owner_channel_revision(target.name) != owner_revision)
                                or (request.pause_requested and request.pause_requested())):
                            self.store.cancel_pending_outbox(target.name, "owner_message_superseded_outbox")
                            self.outbox_changed.set()
                        result = {**await self.bubble_delivery.wait_reply(request.turn_id, call.id), "mode": mode}
                    else:
                        result = {**delivery.result, "bubbles": [], "mode": mode}
                    if delivery.bubbles is not None:
                        batch.last_sent_bubbles = copy.deepcopy(result["bubbles"])
                        batch.last_sent_channel = delivery.channel
            except ValueError as error:
                result = {"ok": False, "error": "invalid_reply", "message": str(error)}
            except Exception as error:
                logger.exception("reply_generation_failed")
                result = {"ok": False, "error": "reply_generation_failed", "message": type(error).__name__}
        action_bubbles = self.store.reply_action_bubbles(request.turn_id, call.id)
        if action_bubbles:
            result["bubbles"] = action_bubbles + list(result.get("bubbles", []))
            if result.get("state") == "no_reply":
                result["state"] = "sent"
        if result.get("ok") and result.get("bubbles"):
            visible_ids = set(request.draft.memory_context)
            for message in request.messages:
                visible_ids.update(message.get("_memory_snapshot", {}).get("access_ids", []))
            request.draft.record_memory_access("used",
                [identifier for identifier in call.arguments.get("used_memory_ids", []) if identifier in visible_ids],
                event_key=call.id)
        attach_reply_episode_triggers(
            self.store, result, stage=execution.stage, seen=request.draft.triggered_episode_ids,
        )
        return result

    async def _qq_recall_message(
        self, call: ToolCall, request: ToolBatchRequest, batch: _BatchEffects,
    ) -> dict[str, Any]:
        result = self.store.begin_tool_call(request.turn_id, call.id, call.name,
                                            call.arguments, "external_effect")
        if result is None:
            try:
                if request.delivery_channel.name != "onebot":
                    raise ValueError("此工具仅适用于当前 QQ 私聊")
                outbox_id, message_id = self.store.recallable_delivery(
                    "onebot", call.arguments.get("outbox_id"))
                if self.store.message_recall_recorded("onebot", message_id):
                    result = {"ok": True, "message_id": message_id, "recalled": True}
                else:
                    await request.delivery_channel.recall_message(message_id)
                    self.store.record_message_recall(MessageRecalled(
                        f"onebot:sent:recall:{message_id}", message_id, time.time(),
                        "onebot", "assistant"))
                    result = {"ok": True, "message_id": message_id, "recalled": True}
                batch.external_effect = True
            except Exception as error:
                result = {"ok": False, "error": "qq_recall_failed", "message": str(error)}
            self.store.complete_tool_call(request.turn_id, call.id, result)
        return result

    async def execute(self, request: ToolBatchRequest) -> ToolBatchResult:
        execution = request.execution
        state = request.state
        batch = _BatchEffects(
            previous_tool_name=state.previous_tool_name,
            last_sent_bubbles=state.last_sent_bubbles,
            last_sent_channel=state.last_sent_channel,
        )
        last_tool_error = ""
        protocol_error = False

        request.messages.append(
            assistant_history_message(request.response.content, request.response.continuation)
        )
        results: list[dict[str, Any]] = []
        image_blocks: list[dict[str, Any]] = []
        image_references: list[dict[str, Any]] = []
        completed_calls: list[tuple[str, bool]] = []
        allowed_tool_names = {str(spec["name"]) for spec in request.request_tools}

        async def execute_call(call: ToolCall) -> str:
            nonlocal last_tool_error, protocol_error
            owned_by_workflow = request.workflow is not None and call.name in request.workflow.tool_names
            source = (
                "workflow"
                if owned_by_workflow
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
            first_call_reply = (
                execution.stage == "owner" and request.delivery_channel.name == "qq_call"
                and call.name == "reply" and call.arguments.get("mode") == "voice"
                and call.arguments.get("channel", "qq_call") == "qq_call"
                and not batch.last_sent_bubbles and "reply" not in request.harness.completed_tools
            )
            validation_error = None
            if (
                not call.argument_error
                and call.name in allowed_tool_names
                and source != "mcp"
            ):
                spec = next(
                    (item for item in request.request_tools if item.get("name") == call.name),
                    None,
                )
                if spec and isinstance(spec.get("input_schema"), dict):
                    normalized, validation_error = validate_tool_arguments(
                        call.name, call.arguments, spec["input_schema"]
                    )
                    if normalized is not None:
                        call = replace(call, arguments=normalized)
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
            elif validation_error:
                result = validation_error
            elif request.pause_requested and request.pause_requested():
                result = {"ok": False, "error": "paused_for_owner",
                          "message": "用户开始聊天，本次调用尚未执行。任务已暂停；处理用户消息后会用最新上下文重新判断，勿自动补发或重放。"}
            elif batch.owner_updates and call.name in {"reply", "wait"} and not first_call_reply:
                result = {
                    "ok": False, "error": "superseded_by_owner_update",
                    "message": f"用户在执行前补充了新消息，本次 {call.name} 未执行。请阅读后续用户插话再决定下一步；其他工具以各自结果为准，运行时没有接管未完成任务。",
                }
            elif call.name == "wait":
                result = await self._wait(call, request, batch, image_blocks)
            elif call.name in PLANNER_SUBAGENT_TOOL_NAMES:
                result = self._subagent_tool(call, request)
            elif call.name == "heartbeat_begin":
                with _tool_log_context(request, call):
                    result = await begin_heartbeat(
                        call,
                        heartbeat_turn=execution.heartbeat,
                        harness_started=request.harness.started,
                    )
            elif call.name == "affinity_change" and not owned_by_workflow:
                # Reflection owns settlement context and completion bookkeeping.
                from .affinity import review_affinity
                limit_error = request.harness.affinity_limit_error(call.arguments.get("direction"), self.store.affinity_daily_increases())
                result = ({"ok": False, "allowed": False, "error": "affinity_daily_limit", "reason": limit_error}
                          if limit_error else await review_affinity(self.config, self.store, self.provider, call, request))
            elif call.name == "heartbeat_activity":
                result = record_heartbeat_activity(
                    call, heartbeat_turn="heartbeat_activity" in request.harness.spec.required_before_end,
                    draft=request.draft,
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
            elif call.name == "recall" and not owned_by_workflow:
                result = await self._recall(call, request)
            elif call.name == "episode_relations":
                try:
                    result = {"ok": True, "cursor": call.arguments.get("cursor", 0),
                              "limit": call.arguments.get("limit", 20), **self.store.episode_relation_graph(
                        str(call.arguments["episode_id"]),
                        call.arguments.get("depth", 1),
                    )}
                except (KeyError, TypeError, ValueError) as error:
                    result = {"ok": False, "error": "invalid_episode_relations", "message": str(error)}
            elif call.name == "phone_result" and not owned_by_workflow:
                result = {"ok": False, "error": "phone_sidecar_not_active",
                          "message": "当前不是电话边车，按当前阶段正常回应或结束。"}
            elif call.name == "reply":
                result = await self._reply(call, request, batch, first_call_reply=first_call_reply)
            elif call.name == "qq_recall_message":
                result = await self._qq_recall_message(call, request, batch)
            elif call.name == "qq_call_status":
                phone = self.bubble_delivery.channels.get("qq_call")
                result = {"ok": True, **(phone.status if phone else {"phase": "disabled"})}
            elif call.name == "tool_search":
                result = search_tools(
                    call,
                    enable_tool_groups=request.enable_tool_groups,
                    tool_surface=self.tool_surface,
                )
            elif call.name == "tool_enable":
                result = enable_tools(
                    call, enable_tool_groups=request.enable_tool_groups,
                    tools=request.tools, tool_surface=self.tool_surface,
                    persist=execution.stage != "subagent",
                )
            elif call.name == "mcp_reload":
                if execution.allowed_capabilities is not None and "external_effect" not in execution.allowed_capabilities:
                    result = {"ok": False, "error": "tool_not_allowed"}
                else:
                    result = await self.tool_surface.mcp.reload()
                    batch.external_effect = True
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
            elif owned_by_workflow:
                # Workflow tools may invoke nested models (e.g. cue admission).
                # The preceding model round's logging scope has already ended.
                with _tool_log_context(request, call):
                    if call.name == "subagent_finish" and call.arguments.get("outcome") == "succeeded" and any(
                        block.get("is_error") for block in results
                    ):
                        result = {"ok": False, "error": "verify_failed_batch_before_finishing"}
                    else:
                        result = await request.workflow.execute_tool(call)
            elif self.tool_executor.is_external(call.name):
                result = None
                if not call.id:
                    result = {"ok": False, "error": "missing_tool_call_id"}
                if result is None:
                    with _tool_log_context(request, call):
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
                    batch.external_effect = batch.external_effect or call_has_external_effect
            elif self.agenda_tools.has_tool(
                call.name
            ):
                result = self.agenda_tools.execute(
                    call,
                    request.draft,
                    source_event_id=request.source_event_id,
                )
            elif source == "memory":
                result = await self.memory_tools.execute_async(
                    call, request.current_events, request.draft, turn_id=request.turn_id
                )
            elif source == "image" and call.name == "save_image_summary":
                result = self.store.save_image_summary(
                    str(call.arguments.get("image_id") or ""), call.arguments.get("summary")
                )
            elif source == "image":
                path = call.arguments.get("path")
                identifier = str(path if path else call.arguments.get("image_id") or "")
                image = (("invalid_arguments" if not identifier else self.store.read_image_file(identifier)) if path
                         else self.store.read_image(identifier) if identifier else "invalid_arguments")
                error = image if isinstance(image, str) else "image_not_found" if image is None else None
                result = {"ok": error is None, **({"path": identifier} if path else {"image_id": identifier})}
                if error:
                    result["error"] = error
                else:
                    # Keep binary input outside JSON results, result storage and logs.
                    # Both providers accept image blocks in this user-role message.
                    image_blocks.extend([
                        {"type": "text", "text": f"[read_image {'path' if path else 'attachment id'}={identifier}]"},
                        image,
                    ])
            elif source == "thinking":
                result = self.thinking_tools.execute(call)
            else:
                result = {"ok": False, "error": "tool_not_allowed"}

            # Schema/dispatch failures are protocol errors; external failures and
            # unsuccessful searches remain execution feedback.
            if (call.argument_error or validation_error
                    or result.get("error") == "tool_not_allowed"
                    or (source not in {"mcp", "builtin"} and str(result.get("error", "")).startswith("invalid_"))):
                protocol_error = True
            recall_result = copy.deepcopy(result) if call.name == "recall" else None
            if "provenance" not in result:
                result = self.tool_executor.normalize(call, result, source)
            if result.get("ok"):
                request.harness.accept(call.name)
                last_tool_error = ""
            else:
                last_tool_error = str(
                    result.get("message") or result.get("error") or "tool_failed"
                )
            self.tool_executor.finish_trace(
                trace, call, recall_result if recall_result is not None else result, request.draft,
            )
            results.append(tool_result_block(call.id, result))
            if source == "mcp":
                if result.get("image_warning"):
                    warning = {"type": "text", "text": f"[tool_call_id={call.id}] {result['image_warning']}"}
                    image_blocks.append(warning)
                    image_references.append(warning)
                image_blocks.extend(attachment_blocks(self.store, call.id, result.get("image_ids", [])))
                image_references.extend({"type": "text", "text": (
                    f"[MCP image attachment; tool_call_id={call.id}; image_id={identifier}; "
                    "use read_image to inspect visual details.]"
                )} for identifier in result.get("image_ids", []))
            if result.get("error") not in {"paused_for_owner", "superseded_by_owner_update"}:
                completed_calls.append((call.name, bool(result.get("ok"))))
            batch.previous_tool_name = call.name
            return last_tool_error

        def parallel_safe(call: ToolCall) -> bool:
            if execution.stage != "owner" or request.workflow is not None:
                return False
            if call.argument_error or call.name not in allowed_tool_names:
                return False
            if call.name == "reply" or call.name in PARALLEL_HISTORY_READS:
                return True
            if not self.tool_executor.is_external(call.name):
                return False
            source = self.tool_executor.source(call.name)
            capability = (self.tool_executor.mcp.capability(call.name) if source == "mcp"
                          else self.tool_executor.builtin_tools.capability(call))
            return capability == "read"

        calls = request.response.tool_calls
        index = 0
        while index < len(calls):
            # Keep submitted work; only stale conversational actions need replanning.
            if execution.accept_owner_updates:
                batch.owner_updates.extend(await request.settle_owner_updates(
                    request.current_events, request.delivery_channel.name
                ))
            group = [calls[index]]
            if parallel_safe(group[0]):
                while index + len(group) < len(calls) and len(group) < 4:
                    candidate = calls[index + len(group)]
                    if not parallel_safe(candidate) or (
                        candidate.name == "reply" and any(item.name == "reply" for item in group)
                    ):
                        break
                    group.append(candidate)
            result_start = len(results)
            if len(group) == 1:
                await execute_call(group[0])
            else:
                # Structured cancellation: /stop must not leave background tool tasks.
                async with asyncio.TaskGroup() as tasks:
                    running = [tasks.create_task(execute_call(item)) for item in group]
                order = {item.id: position for position, item in enumerate(group)}
                results[result_start:] = sorted(
                    results[result_start:], key=lambda block: order[block["tool_use_id"]]
                )
                batch.previous_tool_name = group[-1].name
                last_tool_error = running[-1].result()
            index += len(group)
            call = group[-1]
            result = {"ok": not results[-1]["is_error"]}
            if request.workflow is not None and request.workflow.is_complete():
                results.extend(
                    tool_error_block(pending.id, "workflow_already_completed")
                    for pending in calls[index:]
                )
                break
            if call.name == "wait" and result.get("ok"):
                # Always return to the model before any action planned before waiting.
                results.extend(tool_error_block(pending.id, "reconsider_after_wait")
                               for pending in calls[index:])
                break
            if execution.accept_owner_updates:
                batch.owner_updates.extend(await request.settle_owner_updates(
                    request.current_events, request.delivery_channel.name
                ))

        calls_since_reply = state.calls_since_reply
        notice_blocks: list[dict[str, Any]] = []
        if execution.stage == "owner" and not (
                request.workflow is not None and "phone_result" in request.workflow.tool_names):
            # A successful reply anywhere in this batch satisfies the progress reminder.
            if any(name == "reply" and ok for name, ok in completed_calls):
                calls_since_reply = 0
            else:
                calls_since_reply += sum(
                    name != "reply" for name, _ in completed_calls
                )
                if (calls_since_reply // 3 > state.calls_since_reply // 3):
                    text = (
                        '<runtime_notice type="reply_progress_reminder">\n'
                        f'自本轮开始或上次成功 reply 以来，已连续执行 {calls_since_reply} 次非 reply 工具调用，'
                        '尚未向用户发送新的回应。\n'
                        '用户可能已经等待较久。请优先调用 reply，告知目前已确认的部分信息、'
                        '当前进展或仍需核实的事项，再继续后续工作。\n'
                        '不要编造结果、承诺完成时间或重复上一条回应；这不是要求结束任务。\n'
                        '</runtime_notice>'
                    )
                    notice_blocks.append({"type": "text", "text": text})
                    self.store.append_turn_journal(request.turn_id, "runtime_notice", {
                        "type": "reply_progress_reminder", "text": text,
                        "calls_since_reply": calls_since_reply,
                        "call_id": request.call_id, "round": request.round_number,
                    }, trust="runtime")
        request.messages.append({"role": "user", "content": [*results, *image_blocks, *notice_blocks]})
        # Keep the model's actual assistant text and native tool exchange for
        # later Turns. Delivered speech is confirmed separately by the stored
        # conversation rows; a plain assistant text response never implies send.
        if execution.stage in ASSISTANT_EXCHANGE_STAGES:
            self.store.append_turn_journal(
                request.turn_id,
                "assistant_exchange",
                {
                    "call_id": request.call_id,
                    "round": request.round_number,
                    "content": assistant_history_message(request.response.content)["content"],
                    "results": [*results, *image_references],
                },
                trust="runtime",
            )
        return ToolBatchResult(
            results=results,
            owner_updates=batch.owner_updates,
            external_effect=batch.external_effect,
            state=ToolBatchState(
                previous_tool_name=batch.previous_tool_name,
                last_sent_bubbles=batch.last_sent_bubbles,
                last_sent_channel=batch.last_sent_channel,
                calls_since_reply=calls_since_reply,
            ),
            last_tool_error=last_tool_error,
            protocol_error=protocol_error,
            updates_in_result=batch.updates_in_result,
        )
