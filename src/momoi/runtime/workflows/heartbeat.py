import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from ...tools.contracts.agenda import AGENDA_TOOL_SPECS
from ...observability.events import log_event
from ...observability.values import safe_preview
from ...tools.contracts.memory import MEMORY_TOOL_SPECS
from ...tools.contracts.thinking import THINKING_TOOL_SPECS
from ...models import AgentReply, TurnDraft
from ...reply_wait import REPLY_FOLLOWUP_RETRY_SECONDS
from ...storage import estimate_tokens, truncate_tokens
from ..agent import TurnExecutionSpec
from ..context.presentation import (
    heartbeat_activity_lines,
    heartbeat_self_state_lines,
    heartbeat_topic_lines,
)
from ..context.rendering import assemble_recent_external_events
from ..tool_contracts.conversation import heartbeat_end_turn_tool_spec
from ..transcript.building import build_transcript
from ..transcript.rendering import render_messages
from ..turn_support import (
    ExternalToolTurnError,
    context_data_message as _context_data_message,
    pack_user_context as _pack_user_context,
    reconciliation_message as _reconciliation_message,
    turn_tool_names as _turn_tool_names,
)

logger = logging.getLogger("momoi.runtime.turns")


class HeartbeatWorkflow:
    async def _complete_heartbeat_turn(
        self, stop: asyncio.Event, target_channel: str | None = None
    ) -> None:
        state = self.store.self_state()
        conversation = self.store.heartbeat_conversation_snapshot()
        if conversation["owner_busy"]:
            log_event(
                logger,
                logging.INFO,
                "heartbeat_deferred",
                stage="heartbeat",
                reason=conversation["blocked_by"],
            )
            self.store.release_heartbeat_claim(
                self._heartbeat_retry_delay(
                    str(self.store.self_state().get("heartbeat_claim_kind") or "")
                )
            )
            self.agenda_changed.set()
            return
        claim_kind = state.get("heartbeat_claim_kind")
        scheduled_at = (
            state.get("pending_reply_next_check_at")
            if claim_kind == "reply"
            else (
                state.get("heartbeat_claimed_at")
                if claim_kind == "manual"
                else state.get("next_heartbeat_at")
            )
        )
        turn_kind = "reply-followup" if claim_kind == "reply" else "heartbeat"
        turn_id = self._turn_id(turn_kind, scheduled_at)
        turn_state = self.store.begin_turn(
            turn_id,
            "reply_followup" if claim_kind == "reply" else "heartbeat",
            [f"{turn_kind}:{scheduled_at}"],
        )
        if turn_state in {"completed", "cancelled"}:
            self.store.clear_heartbeat_claim()
            return
        if turn_state == "needs_reconciliation" or stop.is_set():
            self.store.release_heartbeat_claim(
                self._heartbeat_retry_delay(str(claim_kind))
            )
            return
        try:
            complete = (
                self._complete_reply_wait
                if claim_kind == "reply"
                else self._complete_heartbeat
            )
            await complete(
                turn_id,
                target_channel,
                owner_event_revision=int(conversation["owner_event_revision"]),
            )
        except ExternalToolTurnError:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage=turn_kind.replace("-", "_"),
                turn_id=turn_id,
                channel=target_channel,
                reason="fatal_error_after_external_tool",
                exc_info=True,
            )
            self.store.open_reconciliation(turn_id, "fatal_error_after_external_tool")
            self.store.commit_autonomous_turn(
                "heartbeat",
                TurnDraft(
                    notification_messages=[_reconciliation_message(turn_id)],
                    notification_key="heartbeat.reconciliation",
                    notification_priority="urgent",
                    notification_reason=(
                        "Autonomous artifact outcome requires owner confirmation."
                    ),
                ),
                turn_id=turn_id,
                notification_channel=target_channel or "",
            )
            self.store.release_heartbeat_claim(
                self._heartbeat_retry_delay(str(claim_kind))
            )
            self.store.record_turn_failure(turn_id, "fatal_error_after_external_tool")
            self.agenda_changed.set()
        except asyncio.CancelledError:
            if self._stop_requested:
                self.store.cancel_turn(turn_id)
            raise
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage=turn_kind.replace("-", "_"),
                turn_id=turn_id,
                channel=target_channel,
                error_type=type(error).__name__,
                exc_info=True,
            )
            self.store.record_turn_failure(turn_id, type(error).__name__)
            self.store.release_heartbeat_claim(
                self._heartbeat_retry_delay(str(claim_kind))
            )
            self.agenda_changed.set()

    def _heartbeat_retry_delay(self, claim_kind: str = "") -> float:
        pending = self.store.pending_owner_reply()
        if claim_kind != "reply" or not pending:
            return self.config.heartbeat.min_interval_seconds
        return REPLY_FOLLOWUP_RETRY_SECONDS

    async def _complete_heartbeat(
        self,
        turn_id: str,
        target_channel: str | None = None,
        *,
        owner_event_revision: int,
    ) -> None:
        delivery_channel = self._channel_for(target_channel or self.channel.name)
        self_context = self.store.self_state_context()
        notification_key = "heartbeat.chat"
        contact_window = self.store.heartbeat_contact_window(
            notification_key, self.config.notifications
        )
        recent_topics: list[dict[str, object]] = []
        topic_tokens = 0
        for episode in self.store.list_recent_episode_directory(8):
            topic = {
                "title": episode["title"],
                "created_timestamp": episode.get("created_timestamp"),
                "updated_timestamp": episode.get("updated_timestamp"),
                "summary": truncate_tokens(
                    str(
                        episode["narrative_summary"] or episode["working_summary"] or ""
                    ),
                    160,
                ),
                "topics": episode["topics"],
                "entities": episode["entities"],
                "open_loops": episode["open_loops"],
            }
            size = estimate_tokens(json.dumps(topic, ensure_ascii=False))
            if recent_topics and topic_tokens + size > 1200:
                break
            recent_topics.append(topic)
            topic_tokens += size
        goals = self.store.active_goals_context(authority="agent")
        recent_memories = self.store.recent_memory_context()
        long_term_memories = self.store.always_memory_context()
        conversation_rows = self._recent_conversation_rows()
        tool_activity = self.store.turn_activity(
            [str(row["turn_id"]) for row in conversation_rows]
        )
        transcript = build_transcript(
            conversation_rows,
            timezone=self.store.timezone,
            tool_activity=tool_activity,
        )
        transcript_messages = render_messages(
            [*transcript.orphaned, *transcript.groups],
            timezone=self.store.timezone,
            tool_activity=tool_activity,
        )
        artifact_root = self.tool_executor.artifact_root.resolve()
        minimum = max(1, int(self.config.heartbeat.min_interval_seconds / 60))
        maximum = max(minimum, int(self.config.heartbeat.max_interval_seconds / 60))
        heartbeat_event = (
            "[Trusted autonomous heartbeat generated by Momoi. This is not owner speech "
            "or new authority for external side effects.]\n"
            f"Allowed next_check_minutes: {minimum}-{maximum}\n"
            f"Autonomous artifact directory: {artifact_root}\n"
            "Use the supplied context to decide how to inhabit this heartbeat first and owner contact second. "
            "Use tools before claiming searches, observations, file work, or other results."
        )
        current_input = _pack_user_context(
            ("workflow_contract", self._heartbeat_system_prompt()),
            ("autonomous_heartbeat", heartbeat_event),
            (
                "runtime_state",
                (
                    f"Current local time: {datetime.now(self.store.timezone).isoformat(timespec='seconds')}\n"
                    f"{heartbeat_self_state_lines(self_context)}"
                ),
            ),
            ("active_goals", goals),
            (
                "recent_topic_reference",
                heartbeat_topic_lines(recent_topics),
            ),
            (
                "recent_heartbeat_activities",
                heartbeat_activity_lines(self.store.recent_heartbeat_activities()),
            ),
            (
                "recent_external_events",
                assemble_recent_external_events(self.store),
            ),
        )
        system = self._system()
        context_message = _context_data_message(
            ("long_term_memories", long_term_memories),
            ("recent_memories", recent_memories),
            required=True,
        )
        assert context_message is not None
        messages: list[dict[str, Any]] = [
            context_message,
            *transcript_messages,
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": current_input,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]
        tools = [
            *MEMORY_TOOL_SPECS,
            *THINKING_TOOL_SPECS,
            *AGENDA_TOOL_SPECS,
            *self.tool_surface.heartbeat_external_specs(),
            self.tool_surface.send_bubbles_spec(delivery_channel.name),
            heartbeat_end_turn_tool_spec(),
        ]
        draft = TurnDraft()
        memory_events = self.store.recent_owner_events(
            max(20, self.config.transcript_turns_max)
        )
        reply = await self._run_tool_loop(
            system,
            messages,
            tools,
            memory_events,
            draft,
            execution=TurnExecutionSpec(
                "heartbeat",
                allowed_capabilities=frozenset({"read", "write", "external_effect"}),
                artifact_root=artifact_root,
            ),
            source_event_id=f"heartbeat:{turn_id}",
            turn_id=turn_id,
            heartbeat_owner_event_revision=owner_event_revision,
            heartbeat_notification_key=notification_key,
            delivery_channel=delivery_channel,
        )
        if not isinstance(reply, AgentReply) or reply.heartbeat is None:
            raise RuntimeError("Heartbeat Turn ended without end_turn heartbeat state")
        decision = {
            **reply.heartbeat,
            "messages": reply.messages,
            "reply_expectation": reply.reply_expectation,
            "schedule_reply_wait": reply.should_schedule_reply_wait,
            "reply_wait_minutes": reply.reply_wait_delay_minutes,
            "reply_wait_reason": reply.reply_wait_reason,
            "mood_update": reply.mood_update,
        }
        if not contact_window["allowed"]:
            decision["messages"] = []
            decision["reply_expectation"] = ""
            decision["schedule_reply_wait"] = False
            decision["reply_wait_minutes"] = 0
            decision["reply_wait_reason"] = ""
        committed_messages = self.store.commit_heartbeat(
            turn_id,
            owner_event_revision=owner_event_revision,
            notification_config=self.config.notifications,
            activity=decision["activity"],
            result=decision["result"],
            next_heartbeat_at=time.time() + decision["next_check_minutes"] * 60,
            mood_update=decision["mood_update"],
            messages=decision["messages"],
            reason=decision["reason"],
            reply_expectation=(
                decision["reply_expectation"] if decision["schedule_reply_wait"] else ""
            ),
            reply_wait_minutes=decision["reply_wait_minutes"],
            reply_wait_reason=decision["reply_wait_reason"],
            draft=draft,
            memory_events=memory_events,
            notification_channel=delivery_channel.name,
        )
        self.agenda_changed.set()
        if committed_messages:
            self.outbox_changed.set()
        log_event(
            logger,
            logging.INFO,
            "turn_complete",
            stage="heartbeat",
            turn_id=turn_id,
            channel=delivery_channel.name,
            activity=decision["activity"],
            result=safe_preview(decision["result"], 500),
            visible_messages=committed_messages,
            tools=_turn_tool_names(draft),
            tool_calls=len(draft.tool_calls),
            memories=len(draft.memories),
            forgotten_memories=len(draft.forgotten_memories),
            goals=len(draft.goals),
            next_minutes=decision["next_check_minutes"],
            llm=self.store.turn_usage(turn_id),
        )
