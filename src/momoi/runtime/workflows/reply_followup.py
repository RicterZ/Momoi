from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ...models import AgentReply, TurnDraft
from ..agent import TurnExecutionSpec
from ..context.current_state import pack_current_turn_context
from ..context.presentation import heartbeat_self_state_lines
from ..transcript.rendering import owner_idle_gap_message


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
        shared = self.shared_turn_context(turn_id)
        conversation_rows = shared["rows"]
        transcript_messages = shared["history"]
        idle_gap = owner_idle_gap_message(
            conversation_rows,
            now=datetime.now(self.store.timezone).timestamp(),
            timezone=self.store.timezone,
        )
        current_input = pack_current_turn_context(
            self.store, "reply_followup",
            ("workflow_contract", self._reply_wait_system_prompt()),
            (
                "followup",
                f"<followup parent_turn_id={quoteattr(str(pending.get('source_turn') or ''))} "
                f"silent_minutes={quoteattr(str(max(0, int(pending.get('waiting_minutes') or 0))))}>"
                f"<reason>{escape(str(pending.get('reason') or '').strip())}</reason></followup>",
            ),
            (
                "self_state",
                heartbeat_self_state_lines(
                    self.store.self_state_context(),
                    current_time=datetime.now(self.store.timezone).isoformat(timespec="seconds"),
                ),
            ),
        )
        system = self._system(planner=True)
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
        draft = TurnDraft(
            memory_context=shared["memories"],
            memory_conversation=transcript_messages,
        )
        reply = await self._run_tool_loop(
            system,
            messages,
            self.tool_surface.conversation_specs(),
            [],
            draft,
            execution=TurnExecutionSpec(
                "reply_followup",
                permitted_tools=self.tool_surface.permitted_names("reply_followup"),
            ),
            source_event_id=f"reply-followup:{turn_id}",
            turn_id=turn_id,
            heartbeat_owner_event_revision=owner_event_revision,
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
            draft=draft,
            notification_channel=delivery_channel.name,
        )
        self.agenda_changed.set()
        self.outbox_changed.set()
