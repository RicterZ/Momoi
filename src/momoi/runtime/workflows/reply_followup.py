from datetime import datetime
from typing import Any

from ...models import AgentReply, TurnDraft
from ..agent import TurnExecutionSpec
from ..context_service import _heartbeat_self_state_lines
from ..protocol import END_TURN_TOOL_SPEC
from ..transcript import build_transcript, render_messages
from ..turn_support import (
    context_data_message as _context_data_message,
    pack_user_context as _pack_user_context,
)


class ReplyFollowupWorkflow:
    async def _complete_reply_wait(
        self,
        turn_id: str,
        target_channel: str | None = None,
        *,
        owner_event_revision: int,
    ) -> None:
        pending = self.store.pending_owner_reply()
        if pending is None:
            self.store.clear_heartbeat_claim()
            self.store.cancel_turn(turn_id)
            return
        delivery_channel = self._channel_for(
            target_channel or str(pending.get("channel") or self.channel.name)
        )
        notification_key = "heartbeat.reply_followup"
        long_term_memories = self.store.always_memory_context()
        recent_memories = self.store.recent_memory_context()
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
        current_input = _pack_user_context(
            ("workflow_contract", self._reply_wait_system_prompt()),
            (
                "followup",
                f"reason: {str(pending.get('reason') or '').strip()}\n"
                f"silent_minutes: {max(0, int(pending.get('waiting_minutes') or 0))}",
            ),
            (
                "runtime_state",
                (
                    f"Current local time: {datetime.now(self.store.timezone).isoformat(timespec='seconds')}\n"
                    f"{_heartbeat_self_state_lines(self.store.self_state_context())}"
                ),
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
        reply = await self._run_tool_loop(
            system,
            messages,
            [
                self.tool_surface.send_bubbles_spec(delivery_channel.name),
                END_TURN_TOOL_SPEC,
            ],
            [],
            TurnDraft(),
            execution=TurnExecutionSpec("reply_followup"),
            source_event_id=f"reply-followup:{turn_id}",
            turn_id=turn_id,
            heartbeat_owner_event_revision=owner_event_revision,
            heartbeat_notification_key=notification_key,
            delivery_channel=delivery_channel,
        )
        if reply is None:
            self.store.clear_heartbeat_claim()
            self.store.cancel_turn(turn_id)
            return
        if not isinstance(reply, AgentReply):
            raise RuntimeError("Reply follow-up Turn ended without end_turn state")
        self.store.commit_reply_followup(
            turn_id,
            owner_event_revision=owner_event_revision,
            notification_config=self.config.notifications,
            pending_reply_turn_id=str(pending["source_turn"]),
            reason=str(pending["reason"]),
            mood_update=reply.mood_update,
            notification_channel=delivery_channel.name,
        )
        self.agenda_changed.set()
        self.outbox_changed.set()

