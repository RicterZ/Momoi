import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from ....channel import Channel
from ....context_time import context_timestamp
from ....observability.events import log_event
from ....observability.values import safe_preview
from ....models import AgentReply, IncomingMessage, TurnDraft
from ....llm.errors import ProviderError
from ...agent import TurnExecutionSpec
from ...transcript.building import build_transcript
from ...transcript.rendering import (
    render_delivered_bubble_evidence,
    render_messages,
    turn_labels,
)
from ...context.presentation import heartbeat_self_state_lines
from ...turn_support import (
    ExternalToolTurnError,
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
                f"{heartbeat_self_state_lines(self.store.self_state_context())}",
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
