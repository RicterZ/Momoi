"""Ingress policy for an active Turn when new input arrives."""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from ..models import IncomingMessage
from ..observability.events import log_event

logger = logging.getLogger("momoi.runtime.turns")


OwnerArrival = Literal["merge", "pause", "queue"]


@dataclass(frozen=True)
class TurnIngressPolicy:
    owner_arrival: OwnerArrival = "queue"
    external_arrival: Literal["queue"] = "queue"


TURN_INGRESS = {
    "owner": TurnIngressPolicy("merge"),
    "plan_step": TurnIngressPolicy("pause"),
    "webhook": TurnIngressPolicy(),
    "goal": TurnIngressPolicy(),
    "heartbeat": TurnIngressPolicy(),
    "reply_followup": TurnIngressPolicy(),
    "reflection": TurnIngressPolicy(),
    "weekly_reflection": TurnIngressPolicy(),
    "memory_operation": TurnIngressPolicy(),
    "memory_maintenance": TurnIngressPolicy(),
    "current_state_maintenance": TurnIngressPolicy(),
}


class TurnInterruptionRouter:
    """Apply one stage's declared ingress behavior at the receive boundary."""

    def start_active_turn(
        self, work: Coroutine[Any, Any, Any], *, stage: str, channel: str
    ) -> asyncio.Task[Any]:
        self._stop_requested = False
        self._interrupt_reason = ""
        self._active_turn_stage = stage
        self._active_turn_channel = channel
        self._active_turn = asyncio.create_task(work)
        return self._active_turn

    def finish_active_turn(self) -> None:
        self._active_turn = None
        self._stop_requested = False
        self._interrupt_reason = ""

    def external_arrived(self) -> str:
        """External events retain their own authority and wait for the worker."""
        active = self._active_turn
        if active is None or active.done():
            return "idle"
        action = TURN_INGRESS[self._active_turn_stage].external_arrival
        log_event(logger, logging.INFO, "turn_input_routed",
                  stage=self._active_turn_stage, source="external", action=action)
        return action

    def owner_arrived(self, message: IncomingMessage, *, stop: bool = False) -> str:
        if stop and self.webhooks is not None:
            self.webhooks.stop_active()
        active = self._active_turn
        if active is None or active.done():
            return "idle"
        stage = self._active_turn_stage
        channel = self._channel_for(message.channel).name
        if not stop and self._active_turn_channel != channel:
            return "queue"
        action = "cancel" if stop else TURN_INGRESS[stage].owner_arrival
        log_event(logger, logging.INFO, "turn_input_routed",
                  stage=stage, source="owner", action=action,
                  channel=channel, event_id=message.event_id)
        if action in {"cancel", "pause"}:
            self._interrupt_reason = "owner_stop" if stop else "owner_update"
            self._stop_requested = True
            active.cancel()
            if action == "pause":
                self._interruption_notices.setdefault(channel, []).append(
                    "A Plan step was paused by the latest owner message. Its remaining "
                    "steps will not run automatically; check any action already taken "
                    "before deciding whether to create a new Plan."
                )
        return action
