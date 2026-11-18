from dataclasses import replace

from ...channel import Channel
from ...models import AgentReply, TurnDraft
from ..agent import TurnExecutionSpec
from ..context.current_state import pack_current_turn_context
from ..turn_support import (
    SUBAGENT_REPORT_PROMPT_PATH,
    SUBAGENT_REPORT_SYSTEM_PROMPT,
    WEBHOOK_PROMPT_PATH,
    WEBHOOK_SYSTEM_PROMPT,
    live_prompt as _live_prompt,
)


class WebhookWorkflow:
    async def _complete_webhook_turn(
        self, prompt: str, turn_id: str, channel: Channel | None = None,
        *, parent_turn_id: str | None = None,
    ) -> AgentReply:
        return await self._complete_event_turn(
            "webhook", prompt, turn_id, channel, parent_turn_id=parent_turn_id,
            contract_prompt=(WEBHOOK_PROMPT_PATH, WEBHOOK_SYSTEM_PROMPT),
            task_section="current_webhook_task",
            scoped_memories=self.store.scoped_memory_context("webhook"),
        )

    async def _complete_subagent_report_turn(
        self, prompt: str, turn_id: str, channel: Channel | None = None,
        *, parent_turn_id: str | None = None, heartbeat_turn_id: str = "",
    ) -> AgentReply:
        required = frozenset({"heartbeat_activity"}) if heartbeat_turn_id else frozenset()
        return await self._complete_event_turn(
            "subagent_report", prompt, turn_id, channel, parent_turn_id=parent_turn_id,
            contract_prompt=(SUBAGENT_REPORT_PROMPT_PATH, SUBAGENT_REPORT_SYSTEM_PROMPT),
            task_section="worker_report",
            scoped_memories="",
            required_before_end=required,
        )

    async def _complete_event_turn(
        self, stage: str, prompt: str, turn_id: str, channel: Channel | None,
        *, parent_turn_id: str | None, contract_prompt: tuple, task_section: str,
        scoped_memories: str, required_before_end: frozenset[str] = frozenset(),
    ) -> AgentReply:
        channel = channel or self.channel
        state = self.store.begin_turn(turn_id, stage, [turn_id], parent_turn_id=parent_turn_id)
        if state in {"completed", "cancelled", "needs_reconciliation"}:
            raise RuntimeError(f"{stage} turn is {state}")
        shared = self.shared_turn_context(turn_id)
        recent_events = self.recent_message_refs(shared, "event", "E")
        def build_input():
            return pack_current_turn_context(
                self.store, stage,
                ("scoped_memories", scoped_memories),
                ("workflow_contract", _live_prompt(*contract_prompt)),
                (task_section, "\n".join(filter(None, (prompt, self.owner_idle_gap(shared))))),
                self.self_state_section(),
                ("recent_events", recent_events),
            )

        current_input = build_input()
        system = self._system(planner=True)
        messages = self.autonomous_turn_messages(shared, current_input)
        draft = TurnDraft()
        reply = await self._run_tool_loop(
            system,
            messages,
            self.tool_surface.conversation_specs(),
            [],
            draft,
            resume_input=build_input,
            execution=TurnExecutionSpec(
                stage,
                permitted_tools=self.tool_surface.permitted_names(stage) | required_before_end,
                required_before_end=required_before_end,
            ),
            source_event_id=turn_id,
            turn_id=turn_id,
            delivery_channel=channel,
        )
        if not isinstance(reply, AgentReply):
            raise RuntimeError(f"{stage} Turn ended without completing")
        return replace(
            reply, memory_access=draft.memory_access,
            heartbeat_activity=draft.heartbeat_activity, goals=dict(draft.goals),
        )
