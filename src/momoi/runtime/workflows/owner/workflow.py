import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from ....channel import Channel
from ....observability.events import log_event
from ....observability.values import safe_preview
from ....models import AgentReply, IncomingMessage, TurnDraft
from ....llm.errors import ProviderError
from ...agent import TurnExecutionSpec, WorkflowProtocolError
from ...context.current_state import pack_current_turn_context
from ...context.presentation import heartbeat_self_state_lines
from ...turn_support import (
    ExternalToolTurnError,
    TurnBudgetExceeded,
    owner_content_blocks as _owner_content_blocks,
    provider_failure_message as _provider_failure_message,
    reconciliation_message as _reconciliation_message,
    turn_tool_names as _turn_tool_names,
)

logger = logging.getLogger("momoi.runtime.turns")


class OwnerWorkflow:
    def _owner_update_message(
        self,
        updates: list[IncomingMessage],
        channel: Channel,
        recalled: dict[str, str],
    ) -> dict[str, Any]:
        """Carry only what the interruption actually changed.

        The conversation so far is already present as native messages and as
        this Turn's own tool exchanges, and durable memory has not moved, so
        repeating either would duplicate context mid-Turn. What is new is the
        evidence recalled for the revised input, the state that advanced while
        the Turn ran, and the owner's latest words.
        """

        runtime_text = pack_current_turn_context(
            self.store, "owner",
            ("workflow_contract", self._owner_system_prompt()),
            (
                "runtime_directives",
                "[Trusted runtime update received while the previous operation was "
                "running. Re-evaluate the next action and any planned reply using "
                "the owner's latest intent. Prior tool results and any successful "
                "opening recall remain valid in this Turn. Recall again if the latest "
                "intent needs additional evidence. If opening recall has not "
                "succeeded yet, complete it first.]",
            ),
            (
                "self_state",
                heartbeat_self_state_lines(
                    current_time=datetime.now(self.store.timezone).isoformat(timespec="seconds"),
                ),
            ),
            ("recall_memories", recalled["recall_memories"]),
            ("recall_status", recalled["query_recall"]),
            ("reflection_memories", recalled["reflection_memories"]),
            ("episode_directory", recalled["episodes"]),
            include_empty=True,
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
        try:
            try:
                await self._complete_batch(batch, turn_id)
            except (ExternalToolTurnError, WorkflowProtocolError, TurnBudgetExceeded, asyncio.CancelledError):
                raise
            except Exception as error:
                if self.store.turn_has_external_effect(turn_id):
                    raise ExternalToolTurnError(type(error).__name__) from error
                raise
            return
        except ExternalToolTurnError:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="owner",
                turn_id=turn_id,
                channel=channel.name,
                reason="fatal_error_after_external_tool",
                exc_info=True,
            )
            self.store.open_reconciliation(turn_id, "fatal_error_after_external_tool")
            failure_message = _reconciliation_message(turn_id)
            failure_reason = "fatal_error_after_external_tool"
        except TurnBudgetExceeded as error:
            log_event(
                logger,
                logging.WARNING,
                "turn_failure",
                stage="owner",
                turn_id=turn_id,
                channel=channel.name,
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
            )
            failure_message = (
                "This task reached its per-turn processing limit, so I stopped to "
                "avoid further usage. Ask me to continue when ready."
            )
            failure_reason = type(error).__name__
        except WorkflowProtocolError as error:
            log_event(
                logger,
                logging.WARNING,
                "turn_failure",
                stage="owner",
                turn_id=turn_id,
                channel=channel.name,
                layer="protocol",
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
            )
            failure_message = (
                "这次处理连续出错，已经停下了，没能完成。"
            )
            failure_reason = type(error).__name__
        except asyncio.CancelledError:
            raise
        except ProviderError as error:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="owner",
                turn_id=turn_id,
                channel=channel.name,
                layer="provider",
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
            )
            failure_message = _provider_failure_message(error)
            failure_reason = type(error).__name__
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="owner",
                turn_id=turn_id,
                channel=channel.name,
                layer="runtime",
                error_type=type(error).__name__,
                exc_info=True,
            )
            failure_message = (
                "This turn stopped because of an internal error and was not retried "
                "automatically."
            )
            failure_reason = type(error).__name__
        owner_content = self._render_batch(batch)
        self.store.commit_turn(
            batch,
            owner_content,
            AgentReply([failure_message]),
            turn_id=turn_id,
            target_channel=channel.name,
        )
        self.outbox_changed.set()
        self.store.record_turn_failure(turn_id, failure_reason)

    def _render_batch(self, batch: list[IncomingMessage]) -> str:
        return "\n".join(message.text for message in batch)

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

    async def _complete_batch(
        self,
        batch: list[IncomingMessage],
        turn_id: str,
        channel: Channel | None = None,
    ) -> None:
        channel = channel or self._channel_for(batch[0].channel)
        recalled = self.owner_context_baseline()
        reconciliation_control = self._apply_reconciliation_commands(batch)
        directives: list[str] = []
        directives.extend(self._interruption_notices.pop(channel.name, []))
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
        system = self._system()
        injected_memories = shared["memories"]
        runtime_text = pack_current_turn_context(
            self.store, "owner",
            ("workflow_contract", self._owner_system_prompt()),
            (
                "self_state",
                heartbeat_self_state_lines(
                    self.store.self_state_context(),
                    current_time=datetime.now(self.store.timezone).isoformat(timespec="seconds"),
                ),
            ),
            ("runtime_directives", "\n\n".join(directives)),
            ("recent_recall_context", candidates["recent_recall_context"]),
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
            expects_reply=reply.expects_reply,
            schedule_reply_wait=reply.should_schedule_reply_wait,
            llm=self.store.turn_usage(turn_id),
        )
        self.outbox_changed.set()
        self.agenda_changed.set()
        if self.config.episode_annealing.enabled:
            self._episode_annealing_dirty = True
