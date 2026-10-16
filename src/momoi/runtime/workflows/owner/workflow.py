import asyncio
import logging
import re
import time
from typing import Any

from ....channel import Channel
from ....observability.events import log_event
from ....observability.values import safe_preview
from ....models import AgentReply, IncomingMessage, TurnDraft
from ...agent import TurnExecutionSpec, WorkflowProtocolError
from ...context.triggers import triggered_memory_context
from ...context.trigger_monitor import record_trigger_injections
from ...context.current_state import pack_current_turn_context
from ...turn_support import (
    owner_content_blocks as _owner_content_blocks,
    provider_failure_message as _provider_failure_message,
    reconciliation_message as _reconciliation_message,
    run_classified_turn,
    turn_tool_names as _turn_tool_names,
)

logger = logging.getLogger("momoi.runtime.turns")


class OwnerWorkflow:
    def _record_trigger_injections(self, events, rows):
        if record_trigger_injections(self.store, events, rows, self.channel.name):
            self.outbox_changed.set()

    async def _wait_owner_continuation(self, current_events, channel):
        seconds = self.config.owner_continuation_seconds
        if seconds <= 0 or self._active_turn is None:
            return []
        # A timed-out reply may still be queued; only wait after delivery has settled.
        if self.store._db.execute(
            "SELECT 1 FROM outbox WHERE target_channel=? AND state IN ('pending','sending','ambiguous') LIMIT 1",
            (channel.name,),
        ).fetchone():
            return []
        self._owner_continuation_waiting = True
        self._owner_continuation_preempted = False
        self.agenda_changed.set()
        deadline = asyncio.get_running_loop().time() + seconds
        try:
            while asyncio.get_running_loop().time() < deadline:
                if (self._owner_continuation_preempted or not self.autonomous.empty()
                        or not self.webhook_requests.empty()
                        or self._active_annealing is not None):
                    return []
                while not self.incoming.empty():
                    self._deferred_incoming.append(self.incoming.get_nowait())
                queued = list(self._deferred_incoming)
                # Leave another channel or command queued for the ordinary scheduler.
                if any(event.channel != current_events[0].channel
                       or event.text.strip().startswith("/") for event in queued):
                    return []
                if queued:
                    return self.owner_updates.drain(current_events, channel.name)
                await asyncio.sleep(min(0.05, max(0, deadline - asyncio.get_running_loop().time())))
            return []
        finally:
            self._owner_continuation_waiting = False
            self.agenda_changed.set()

    def _owner_update_message(
        self,
        updates: list[IncomingMessage],
        channel: Channel,
        recalled: dict[str, str],
        *, draft: TurnDraft | None = None,
    ) -> dict[str, Any]:
        """Steer the same turn with matched memories followed by new owner input."""
        triggered = triggered_memory_context(
            self.memory, updates, draft.memory_context if draft is not None else {}, only_new=True,
            on_injected=self._record_trigger_injections,
        )
        runtime_text = (
            (f"<affinity>{self.store.self_state()['affinity']}/100</affinity>\n\n" if self.config.affinity_enabled else "")
            + (f"<triggered_memories>\n{triggered}\n</triggered_memories>\n\n" if triggered else "")
            + "[用户中途插话]"
        )
        content = _owner_content_blocks(
            updates, channel.content_blocks, self.store.timezone, runtime_text
        )
        content[-1]["cache_control"] = {"type": "ephemeral"}
        return {"role": "user", "content": content}

    async def _complete_batch_turn(
        self,
        batch: list[IncomingMessage],
        stop: asyncio.Event,
        turn_id: str,
        channel: Channel | None = None,
    ) -> None:
        channel = channel or self._channel_for(batch[0].channel)
        state = self.store.begin_turn(
            turn_id, "owner", [event.event_id for event in batch]
        )
        if state in {"completed", "cancelled"}:
            self.store.discard_events(batch)
            return
        if state == "needs_reconciliation":
            owner_content = self._render_batch(batch)
            self.store.commit_turn(
                batch,
                owner_content,
                AgentReply([_reconciliation_message(turn_id)]),
                turn_id=turn_id,
                target_channel=channel.name,
            )
            self.outbox_changed.set()
            self.store.record_turn_failure(
                turn_id, "process_interrupted_after_external_effect"
            )
            return
        if stop.is_set():
            return
        failure = await run_classified_turn(
            self.store, self._complete_batch(batch, turn_id),
            stage="owner", turn_id=turn_id, log_fields={"channel": channel.name},
            protocol_errors=(WorkflowProtocolError,),
        )
        if failure is None:
            return
        if failure.kind == "provider":
            failure_message = _provider_failure_message(failure.error)
        else:
            failure_message = {
                "external": _reconciliation_message(turn_id),
                "budget": "这次处理已达到执行上限，已暂停，任务尚未完成。",
                "protocol": "这次处理连续出错，已经停下了，没能完成。",
                "runtime": "处理时发生内部错误，已停止，任务尚未完成。",
            }[failure.kind]
        owner_content = self._render_batch(batch)
        self.store.commit_turn(
            batch,
            owner_content,
            AgentReply([failure_message]),
            turn_id=turn_id,
            target_channel=channel.name,
        )
        self.outbox_changed.set()
        self.store.record_turn_failure(turn_id, failure.reason)

    def _render_batch(self, batch: list[IncomingMessage]) -> str:
        return "\n".join(message.text for message in batch if not message.delivery_context.get("channel_notice"))

    def _apply_reconciliation_commands(self, batch: list[IncomingMessage]) -> str:
        results: list[str] = []
        for message in batch:
            text = message.text.strip()
            if not (text.startswith("/resolve") or text.startswith("/resume")):
                continue
            match = re.fullmatch(
                r"/(resolve|resume)\s+([0-9a-f]{8,32})\s+(.+)", text, re.DOTALL
            )
            if match is None:
                results.append(
                    "Command rejected: expected action, turn id prefix, and confirmed state."
                )
                continue
            action, prefix, resolution = match.groups()
            try:
                item = self.store.resolve_reconciliation(
                    prefix, resolution, resume=action == "resume"
                )
                results.append(
                    f"turn_id={item['turn_id']} status={item['status']} "
                    f"owner_resolution={item['resolution']}"
                )
            except ValueError as error:
                results.append(f"Command rejected: {error}")
        return "\n".join(results)

    def _phone_planner_context(self, batch, turn_id, channel):
        self.owner_context_baseline()
        shared = self.shared_turn_context(turn_id)
        memories = shared["memories"]
        candidates = self.owner_context_candidates([str(row["turn_id"]) for row in shared["rows"]])
        runtime_text = pack_current_turn_context(
            self.store, "owner",
            ("workflow_contract", self._owner_system_prompt()),
            self.self_state_section(),
            ("runtime_directives",
             "当前消息渠道：qq_call（QQ 电话）。"
             "reply 省略 channel 时回复此渠道；历史对话或默认渠道不代表当前消息来源。\n\n"
             "用户正在和你进行 QQ 对话，你现在的角色是“电话执行者”。"
             "电话回复器已经承担了回复的任务，你只需要根据用户消息进行执行判断，"
             "不要调用 reply、wait 等回复工具，不要代写台词。\n\n"
             "你只负责判断是否需要执行实际操作："
             "如果不需要执行工具，调用 phone_result，result 留空；"
             "用户提出操作请求时，调用现有工具完成操作；"
             "操作成功、失败或无法执行后，调用 phone_result。"
             "phone_result.result 只填写真实结果、失败原因和必要事实，不写给用户的台词，"
             "前台回复器会根据你的结论再次讲话。"),
            ("recent_recall_context", candidates["recent_recall_context"]),
            ("triggered_memories", triggered_memory_context(
                self.memory, batch, memories, on_injected=self._record_trigger_injections)),
        )
        content = _owner_content_blocks(batch, channel.content_blocks, self.store.timezone, runtime_text)
        content[-1]["cache_control"] = {"type": "ephemeral"}
        return [*shared["messages"], {"role": "user", "content": content}], TurnDraft(
            memory_context=memories, memory_conversation=shared["history"])

    async def _phone_planner(self, batch, turn_id, channel):
        from ...agent.workflow import AgentWorkflow
        reports = []
        done = False

        async def report(call):
            nonlocal done
            reports.append(call.arguments)
            done = True
            return {"ok": True, "forwarded_to_phone_replyer": True}

        permitted = self.tool_surface.permitted_names("owner")
        tools = self.tool_surface.conversation_specs()
        messages, draft = self._phone_planner_context(batch, turn_id, channel)
        workflow = AgentWorkflow(
            stage="owner", tool_names=frozenset({"phone_result"}), execute_tool=report,
            is_complete=lambda: done, completion_result=lambda: {"ok": True},
            no_tool_correction="请执行必要操作后调用 phone_result；纯聊天 result 留空。",
        )
        await self._run_tool_loop(
            self._system(planner=True),
            messages,
            tools, batch, draft,
            execution=TurnExecutionSpec("owner", permitted_tools=permitted),
            source_event_id=batch[0].event_id, turn_id=turn_id, delivery_channel=channel,
            workflow=workflow,
        )
        return {"result": reports[-1].get("result", "") if reports else "", "draft": draft}

    async def _complete_phone_batch(self, batch, turn_id, channel):
        started = time.monotonic()
        log_event(logger, logging.INFO, "qq_call_turn_started", channel=channel.name,
                  turn_id=turn_id, executor="phone_replyer")
        context = batch[-1].delivery_context
        if not channel.context_valid(context):
            raise asyncio.CancelledError()
        from ....channel.napcat.voice_call.speech import BufferedBubbleAudio
        provider = self.bubble_delivery.tts_provider
        prefetch = BufferedBubbleAudio(provider) if callable(getattr(provider, "stream_pcm", None)) else None
        if prefetch:
            self.bubble_delivery.phone_prefetch[turn_id] = prefetch
        try:
            await self._run_phone_reply(batch, turn_id, channel, context, started, prefetch)
        finally:
            if prefetch:
                self.bubble_delivery.phone_prefetch.pop(turn_id, None)
                await prefetch.close()

    async def _run_phone_reply(self, batch, turn_id, channel, context, started, prefetch):
        from ...agent.phone_replyer import PhoneReplyer
        deliveries = []

        async def enqueue(paragraph):
            if not channel.context_valid(context):
                raise asyncio.CancelledError()
            if prefetch:
                prefetch.start(paragraph)
                if deliveries:
                    return
            identifier = f"phone-reply-{len(deliveries)}"
            delivery = await self.bubble_delivery.send_voice(
                paragraph, tool_call_id=identifier, turn_id=turn_id,
                delivery_channel=channel, heartbeat_turn=False,
                heartbeat_owner_event_revision=None, delivery_context=context,
            )
            if not delivery.result.get("ok"):
                raise WorkflowProtocolError(str(delivery.result.get("error") or "phone_delivery_rejected"))
            deliveries.append(identifier)

        try:
            async with asyncio.TaskGroup() as tasks:
                planner = tasks.create_task(self._phone_planner(batch, turn_id, channel))
                reaction = tasks.create_task(PhoneReplyer(self.config, self.store, self.provider).generate(
                    batch, turn_id, enqueue))
            text = reaction.result()
            outcome = planner.result()
            result = outcome["result"]
            if result and channel.context_valid(context):
                supplement = await PhoneReplyer(self.config, self.store, self.provider).generate(
                    batch, turn_id, enqueue, operation_result=result)
                text = "\n\n".join(part for part in (text, supplement) if part)
        except BaseException:
            self.store.cancel_pending_outbox(channel.name, "phone_generation_interrupted")
            raise
        finally:
            if prefetch:
                prefetch.finish()
        if prefetch and deliveries:
            # One durable delivery represents the entire continuous audio stream.
            with self.store._db:
                self.store._db.execute("UPDATE turn_progress SET text=? WHERE turn_id=? AND tool_call_id=?",
                                       (text, turn_id, deliveries[0]))
                self.store._db.execute("UPDATE outbox SET text=? WHERE turn_id=? AND dedupe_key LIKE ?",
                                       (text, turn_id, f"turn:{turn_id}:progress:{deliveries[0]}:%"))
        log_event(logger, logging.INFO, "qq_call_reply_generated", channel=channel.name,
                  turn_id=turn_id, elapsed_ms=round((time.monotonic() - started) * 1000),
                  silent=not bool(text))
        if not channel.context_valid(context):
            raise asyncio.CancelledError()
        for identifier in deliveries:
            result = await self.bubble_delivery.wait_reply(turn_id, identifier)
            if not result.get("ok"):
                raise WorkflowProtocolError(result.get("error", "phone_delivery_failed"))
        self.store.append_turn_journal(turn_id, "assistant_exchange", {
            "content": [{"type": "text", "text": text}] if text else [],
            "results": [],
        }, trust="runtime")
        self.store.commit_turn(batch, self._render_batch(batch), AgentReply([]), outcome["draft"],
                               turn_id=turn_id, target_channel=channel.name)
        self.outbox_changed.set()
        if self.config.episode_annealing.enabled:
            self._episode_annealing_dirty = True

    async def _complete_batch(
        self,
        batch: list[IncomingMessage],
        turn_id: str,
        channel: Channel | None = None,
    ) -> None:
        channel = channel or self._channel_for(batch[0].channel)
        if channel.name == "qq_call":
            await self._complete_phone_batch(batch, turn_id, channel)
            return
        prepared_at = time.monotonic()
        if channel.name == "qq_call":
            log_event(logger, logging.INFO, "qq_call_turn_started", channel=channel.name, turn_id=turn_id,
                      recognition_wait_ms=round((time.time() - batch[-1].received_at) * 1000))
        self.owner_context_baseline()
        reconciliation_control = self._apply_reconciliation_commands(batch)
        channel_label = {"dashboard": "网页聊天", "napcat": "QQ 私聊", "weixin": "微信私聊", "qq_call": "QQ 电话"}.get(channel.name, channel.name)
        directives: list[str] = [
            f"当前消息渠道：{channel.name}（{channel_label}）。"
            "reply 省略 channel 时回复此渠道；历史对话或默认渠道不代表当前消息来源。"
        ]
        background = getattr(self, "_background_pause", None)
        if background is not None and background.context:
            directives.append(background.context)
        directives.extend(self._interruption_notices.pop(channel.name, []))
        if channel.name == "qq_call":
            directives.append("当前是主人通过 QQ 电话的发言，使用 reply(mode=voice) 回应；保持自然口语，结束本轮不等于挂断电话。")
        for plan in self.store.paused_task_plans(channel.name):
            if plan["status"] == "paused" and self.store.plan_resume_safety(plan) == "owner_decision_required":
                directives.append(
                    f"计划 {plan['id']} version={plan['version']} 因步骤50次上限暂停，等待用户介入。"
                    "先 plan_get 查看卡点与交接。用户明确要求继续时 plan_resume，owner_feedback 引用本次用户原话；"
                    "从交接继续，不重做已完成或可能已生效的操作。修改则 plan_update 后重新提交审核，终止则 plan_cancel。"
                    "无关聊天不能恢复。"
                )
                continue
            if plan["status"] != "paused":
                directives.append(
                    f"待处理计划：id={plan['id']} title={plan['title']!r} "
                    f"version={plan['version']} status={plan['status']}。"
                    "用 plan_get 查看方案与调研依据。草稿需要继续调查、完善并 plan_submit；"
                    "待审核方案只有用户明确同意才能 plan_start，引用当前消息的同意原文。"
                    "用户提出修改则 plan_update 后重新提交；拒绝或停止则 plan_cancel。"
                    "无关聊天不代表批准，也不自动取消计划。"
                )
                continue
            directives.append(
                f"Paused Plan: id={plan['id']} title={plan['title']!r} "
                f"version={plan['version']} current_step={plan['step_index'] + 1} "
                f"resume_safety={self.store.plan_resume_safety(plan)}. "
                "Use plan_get for the full request and step details if needed. "
                "Resolve the owner's latest intent: if they stopped the work, use plan_cancel; "
                "if they corrected it, use plan_update and plan_submit for a new review; "
                "if this was an unrelated aside, answer it and use plan_resume. "
                "If resume_safety requires review, do not replay the interrupted step; "
                "explain the uncertainty and ask only for information needed to continue safely."
            )
        if any(message.text.strip() == "/stop" for message in batch):
            directives.append(
                "The owner explicitly stopped the previous active task. The runtime has "
                "cancelled it and discarded uncommitted work. Do not continue that task. "
                "Acknowledge the stop naturally; already dispatched external actions are "
                "not automatically undone."
            )
        if reconciliation_control:
            directives.append(reconciliation_control)
        shared = self.shared_turn_context(turn_id)
        conversation_rows = shared["rows"]
        transcript = shared["transcript"]
        transcript_messages = shared["history"]
        candidates = self.owner_context_candidates(
            [str(row["turn_id"]) for row in conversation_rows],
        )
        system = self._system(planner=True)
        injected_memories = shared["memories"]
        runtime_text = pack_current_turn_context(
            self.store, "owner",
            ("workflow_contract", self._owner_system_prompt()),
            self.self_state_section(),
            ("runtime_directives", "\n\n".join(directives)),
            ("recent_recall_context", candidates["recent_recall_context"]),
            ("triggered_memories", triggered_memory_context(self.memory, batch, injected_memories, on_injected=self._record_trigger_injections)),
        )
        current_content = _owner_content_blocks(
            batch, channel.content_blocks, self.store.timezone, runtime_text
        )
        current_content[-1]["cache_control"] = {"type": "ephemeral"}
        messages: list[dict[str, Any]] = [
            *shared["messages"],
            {"role": "user", "content": current_content},
        ]
        if transcript.orphaned:
            log_event(
                logger,
                logging.INFO,
                "transcript_orphaned_proactive_speech",
                turn_id=turn_id,
                groups=len(transcript.orphaned),
                bubbles=sum(len(group.parts) for group in transcript.orphaned),
            )
        draft = TurnDraft(memory_context=injected_memories, memory_conversation=transcript_messages)
        tools = self.tool_surface.conversation_specs()
        if channel.name == "qq_call":
            log_event(logger, logging.INFO, "qq_call_context_prepared", channel=channel.name, turn_id=turn_id,
                      elapsed_ms=round((time.monotonic() - prepared_at) * 1000))
        reply = await self._run_tool_loop(
            system,
            messages,
            tools,
            batch,
            draft,
            execution=TurnExecutionSpec(
                "owner",
                permitted_tools=self.tool_surface.permitted_names("owner"),
            ),
            source_event_id=batch[0].event_id,
            turn_id=turn_id,
            delivery_channel=channel,
        )
        if reply is None:
            raise RuntimeError("Owner Turn ended without end_turn")

        owner_content = self._render_batch(batch)
        self.store.commit_turn(
            batch,
            owner_content,
            reply,
            draft,
            turn_id=turn_id,
            target_channel=channel.name,
        )
        log_event(
            logger,
            logging.INFO,
            "turn_complete",
            stage="owner",
            turn_id=turn_id,
            channel=channel.name,
            events=len(batch),
            owner_text=safe_preview(owner_content, 500),
            visible_messages=len(reply.messages),
            tools=_turn_tool_names(draft),
            tool_calls=len(draft.tool_calls),
            memory_operations=len(draft.memory_operations),
            goals=len(draft.goals),
            llm=self.store.turn_usage(turn_id),
        )
        self.outbox_changed.set()
        self.agenda_changed.set()
        if self.config.episode_annealing.enabled:
            self._episode_annealing_dirty = True
