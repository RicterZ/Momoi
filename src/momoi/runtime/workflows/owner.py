import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from ...channel import Channel
from ...context_time import context_timestamp
from ...logging_context import log_event, safe_preview
from ...models import AgentReply, IncomingMessage, ProviderResponse, TurnDraft
from ...provider import ProviderError
from ..agent import TurnExecutionSpec
from ..transcript import (
    build_transcript,
    render_delivered_bubble_evidence,
    render_messages,
    turn_labels,
)
from ..context_service import (
    _heartbeat_self_state_lines,
)
from ..turn_support import (
    ExternalToolTurnError,
    OwnerMessagesChanged,
    TurnBudgetExceeded,
    owner_content_blocks as _owner_content_blocks,
    owner_context_message as _owner_context_message,
    pack_user_context as _pack_user_context,
    provider_failure_message as _provider_failure_message,
    reconciliation_message as _reconciliation_message,
    turn_tool_names as _turn_tool_names,
)

logger = logging.getLogger("momoi.runtime.turns")


class OwnerWorkflow:
    def _drain_owner_updates(
        self, current_events: list[IncomingMessage], channel_name: str
    ) -> list[IncomingMessage]:
        updates: list[IncomingMessage] = []
        for _ in range(len(self._deferred_incoming)):
            message = self._deferred_incoming.popleft()
            if self._channel_for(message.channel).name == channel_name:
                updates.append(message)
            else:
                self._deferred_incoming.append(message)
        while True:
            try:
                message = self.incoming.get_nowait()
            except asyncio.QueueEmpty:
                break
            if self._channel_for(message.channel).name == channel_name:
                updates.append(message)
            else:
                self._deferred_incoming.append(message)
        if updates:
            current_events.extend(updates)
            log_event(
                logger,
                logging.INFO,
                "owner_updates_injected",
                count=len(updates),
                channel=channel_name,
            )
        return updates

    async def _settle_owner_updates(
        self, current_events: list[IncomingMessage], channel_name: str
    ) -> list[IncomingMessage]:
        channel = self._channel_for(channel_name)
        loop = asyncio.get_running_loop()
        hard_deadline = loop.time() + channel.max_batch_seconds
        updates: list[IncomingMessage] = []
        while True:
            updates.extend(self._drain_owner_updates(current_events, channel.name))
            deadline = min(
                self._owner_quiet_until.get(channel.name, 0.0), hard_deadline
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                return updates
            self._owner_activity_changed.clear()
            try:
                await asyncio.wait_for(
                    self._owner_activity_changed.wait(), timeout=remaining
                )
            except TimeoutError:
                pass

    async def _complete_with_owner_interrupt(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        require_tool: bool,
        current_events: list[IncomingMessage],
        channel_name: str,
    ) -> ProviderResponse:
        initial = self._drain_owner_updates(current_events, channel_name)
        if initial:
            log_event(
                logger,
                logging.INFO,
                "llm_skipped",
                reason="owner_update",
                updates=len(initial),
            )
            raise OwnerMessagesChanged(initial)

        provider_task = asyncio.create_task(
            self.provider.complete(
                system,
                messages,
                tools,
                require_tool=require_tool,
            )
        )
        try:
            while True:
                self._owner_message_changed.clear()
                updates = self._drain_owner_updates(current_events, channel_name)
                if updates:
                    provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    log_event(
                        logger,
                        logging.INFO,
                        "llm_cancelled",
                        reason="owner_update",
                        updates=len(updates),
                    )
                    raise OwnerMessagesChanged(updates)
                if provider_task.done():
                    return provider_task.result()

                changed = asyncio.create_task(self._owner_message_changed.wait())
                done, _ = await asyncio.wait(
                    {provider_task, changed},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if changed not in done:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
                updates = self._drain_owner_updates(current_events, channel_name)
                if updates:
                    provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    log_event(
                        logger,
                        logging.INFO,
                        "llm_cancelled",
                        reason="owner_update",
                        updates=len(updates),
                    )
                    raise OwnerMessagesChanged(updates)
                if provider_task in done:
                    return provider_task.result()
        except asyncio.CancelledError:
            provider_task.cancel()
            await asyncio.gather(provider_task, return_exceptions=True)
            raise

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

        runtime_text = _pack_user_context(
            ("workflow_contract", self._owner_system_prompt()),
            (
                "runtime_directives",
                "[Trusted runtime update received while the previous operation was "
                "running. Re-evaluate the next action and any planned reply using "
                "the owner's latest intent.]",
            ),
            (
                "runtime_state",
                "Current local time: "
                f"{datetime.now(self.store.timezone).isoformat(timespec='seconds')}",
            ),
            ("goal_progress", recalled["goal_progress"]),
            ("recall_memories", recalled["recall_memories"]),
            ("recall_status", recalled["query_recall"]),
            ("reflection_memories", recalled["reflection_memories"]),
            ("episode_directory", recalled["episodes"]),
            ("recent_external_events", recalled["recent_external_events"]),
            (
                "interrupted_reply_expectation",
                self.store.cooled_reply_expectation_context(),
            ),
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
            except (ExternalToolTurnError, TurnBudgetExceeded, asyncio.CancelledError):
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
        return "\n".join(
            f"{context_timestamp(message.occurred_at, self.store.timezone)} {message.text}"
            for message in batch
        )

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
        recalled = self.owner_context_baseline(batch)
        reconciliation_control = self._apply_reconciliation_commands(batch)
        directives: list[str] = []
        if any(message.text.strip() == "/stop" for message in batch):
            directives.append(
                "The owner explicitly stopped the previous active task. The runtime has "
                "cancelled it and discarded uncommitted work. Do not continue that task. "
                "Acknowledge the stop naturally; already dispatched external actions are "
                "not automatically undone."
            )
        if reconciliation_control:
            directives.append(reconciliation_control)
        conversation_rows = self._recent_conversation_rows(
            min(event.received_at for event in batch)
        )
        tool_activity = self.store.turn_activity(
            [str(row["turn_id"]) for row in conversation_rows]
        )
        transcript = build_transcript(
            conversation_rows,
            timezone=self.store.timezone,
            tool_activity=tool_activity,
        )
        transcript_labels = turn_labels(transcript.groups)
        candidates = self.owner_context_candidates(
            [turn for group in transcript.groups for turn in group.turn_ids],
            transcript_labels,
        )
        transcript_messages = render_messages(
            transcript.groups,
            timezone=self.store.timezone,
            tool_activity=tool_activity,
            labels=transcript_labels,
        )
        delivered_proactive_bubbles = render_delivered_bubble_evidence(
            transcript.orphaned,
            timezone=self.store.timezone,
            tool_activity=tool_activity,
        )
        system = self._system()
        # Slow-changing material sits ahead of the transcript so it stays inside
        # the cached prefix; everything that moves with the Turn stays in the
        # tail, which is rebuilt anyway.
        context_message = _owner_context_message(
            ("long_term_memories", recalled["long_term_memories"]),
            ("recent_memories", recalled["recent_memories"]),
            ("goal_directory", recalled["goal_directory"]),
        )
        runtime_text = _pack_user_context(
            ("workflow_contract", self._owner_system_prompt()),
            (
                "runtime_state",
                "Current local time: "
                f"{datetime.now(self.store.timezone).isoformat(timespec='seconds')}\n"
                f"{_heartbeat_self_state_lines(self.store.self_state_context())}",
            ),
            ("runtime_directives", "\n\n".join(directives)),
            ("goal_progress", recalled["goal_progress"]),
            ("delivered_proactive_bubbles", delivered_proactive_bubbles),
            ("candidate_episodes", candidates["candidate_episodes"]),
            ("recent_recall_context", candidates["recent_recall_context"]),
            ("recent_external_events", recalled["recent_external_events"]),
            (
                "interrupted_reply_expectation",
                self.store.cooled_reply_expectation_context(),
            ),
        )
        current_content = _owner_content_blocks(
            batch, channel.content_blocks, self.store.timezone, runtime_text
        )
        current_content[-1]["cache_control"] = {"type": "ephemeral"}
        messages: list[dict[str, Any]] = [
            *([context_message] if context_message else []),
            *transcript_messages,
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
        draft = TurnDraft()
        tools = self._owner_tool_specs(channel.name)
        reply = await self._run_tool_loop(
            system,
            messages,
            tools,
            batch,
            draft,
            execution=TurnExecutionSpec("owner"),
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
            memories=len(draft.memories),
            forgotten_memories=len(draft.forgotten_memories),
            goals=len(draft.goals),
            expects_reply=reply.expects_reply,
            schedule_reply_wait=reply.should_schedule_reply_wait,
            llm=self.store.turn_usage(turn_id),
        )
        self.outbox_changed.set()
        self.agenda_changed.set()
        if self.config.episode_annealing.enabled:
            self.episode_annealing_requested.set()
