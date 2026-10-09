import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any

from ...observability.events import log_event
from ...observability.values import safe_preview
from ...models import AgentReply, TurnDraft
from ..agent import TurnExecutionSpec
from ..context.current_state import pack_current_turn_context
from ..context.presentation import (
    heartbeat_self_state_lines,
)
from ..transcript.rendering import owner_idle_gap_message
from ..turn_support import (
    ExternalToolTurnError,
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
                self.config.heartbeat.min_interval_seconds
            )
            self.agenda_changed.set()
            return
        claim_kind = state.get("heartbeat_claim_kind")
        scheduled_at = (
            state.get("heartbeat_claimed_at")
            if claim_kind == "manual" else state.get("next_heartbeat_at")
        )
        turn_kind = "heartbeat"
        turn_id = self._turn_id(turn_kind, scheduled_at)
        turn_state = self.store.begin_turn(
            turn_id, "heartbeat", [f"heartbeat:{scheduled_at}"],
        )
        if turn_state in {"completed", "cancelled"}:
            self.store.clear_heartbeat_claim()
            return
        if turn_state == "needs_reconciliation" or stop.is_set():
            self.store.release_heartbeat_claim(
                self.config.heartbeat.min_interval_seconds
            )
            return
        try:
            await self._complete_heartbeat(
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
                self.config.heartbeat.min_interval_seconds
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
                self.config.heartbeat.min_interval_seconds
            )
            self.agenda_changed.set()

    async def _complete_heartbeat(
        self,
        turn_id: str,
        target_channel: str | None = None,
        *,
        owner_event_revision: int,
    ) -> None:
        delivery_channel = self._channel_for(target_channel or self.channel.name)
        self_context = self.store.self_state_context()
        contact_window = self.store.heartbeat_contact_window(
            self.config.notifications
        )
        shared = self.shared_turn_context(turn_id)
        conversation_rows = shared["rows"]
        transcript_messages = shared["history"]
        idle_gap = owner_idle_gap_message(
            conversation_rows,
            now=time.time(),
            timezone=self.store.timezone,
        )
        artifact_root = self.tool_executor.artifact_root.resolve()
        heartbeat_event = (
            f"Autonomous artifact directory: {artifact_root}\n"
            "heartbeat_activity.next_check_minutes: integer "
            f"{max(1, math.ceil(self.config.heartbeat.min_interval_seconds / 60))}-"
            f"{min(1440, math.floor(self.config.heartbeat.max_interval_seconds / 60))} minutes."
        )
        current_input = pack_current_turn_context(
            self.store, "heartbeat",
            ("scoped_memories", self.store.scoped_memory_context("heartbeat")),
            ("workflow_contract", self._heartbeat_system_prompt()),
            ("autonomous_heartbeat", heartbeat_event),
            (
                "self_state",
                heartbeat_self_state_lines(
                    self_context,
                    current_time=datetime.now(self.store.timezone).isoformat(timespec="seconds"),
                ),
            ),
        )
        system = self._system(planner=True)
        injected_memories = shared["memories"]
        messages: list[dict[str, Any]] = [
            *shared["messages"],
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": current_input + ("\n\n" + "\n".join(block["text"] for block in idle_gap["content"] if block.get("type") == "text") if idle_gap else ""),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]
        tools = self.tool_surface.conversation_specs()
        draft = TurnDraft(memory_context=injected_memories, memory_conversation=transcript_messages)
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
                permitted_tools=self.tool_surface.permitted_names("heartbeat"),
            ),
            source_event_id=f"heartbeat:{turn_id}",
            turn_id=turn_id,
            heartbeat_owner_event_revision=owner_event_revision,
            delivery_channel=delivery_channel,
        )
        if not isinstance(reply, AgentReply):
            raise RuntimeError("Heartbeat Turn ended without end_turn")
        if draft.heartbeat_activity is None:
            raise RuntimeError("Heartbeat Turn ended without heartbeat_activity")
        decision = {
            **draft.heartbeat_activity,
            "messages": reply.messages,
            "mood_update": reply.mood_update,
        }
        if not contact_window["allowed"]:
            decision["messages"] = []
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
            memory_operations=len(draft.memory_operations),
            goals=len(draft.goals),
            next_minutes=decision["next_check_minutes"],
            llm=self.store.turn_usage(turn_id),
        )
