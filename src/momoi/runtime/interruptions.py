"""Ingress policy for an active Turn when new input arrives."""

import asyncio
import logging
import json
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal

from ..models import IncomingMessage
from ..observability.events import log_event

logger = logging.getLogger("momoi.runtime.turns")


OwnerArrival = Literal["merge", "pause", "yield", "queue"]


@dataclass(frozen=True)
class TurnIngressPolicy:
    owner_arrival: OwnerArrival = "queue"
    external_arrival: Literal["queue"] = "queue"


TURN_INGRESS = {
    "owner": TurnIngressPolicy("merge"),
    "plan_step": TurnIngressPolicy("pause"),
    "webhook": TurnIngressPolicy("yield"),
    "goal": TurnIngressPolicy("yield"),
    "heartbeat": TurnIngressPolicy("yield"),
    "reflection": TurnIngressPolicy(),
    "weekly_reflection": TurnIngressPolicy(),
    "memory_operation": TurnIngressPolicy(),
    "current_state_maintenance": TurnIngressPolicy(),
}


@dataclass
class BackgroundPause:
    task: asyncio.Task
    requested: bool = False
    context: str = ""
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)


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
        parked = getattr(self, "_background_pause", None)
        if stop and parked is not None and parked.task is not active:
            parked.task.cancel()
        if not stop and TURN_INGRESS[stage].owner_arrival == "yield" and parked is not None:
            parked.requested = True
            self.store.cancel_pending_outbox(self._active_turn_channel, "owner_message_superseded_outbox")
            self.outbox_changed.set()
            log_event(logger, logging.INFO, "turn_pause_requested", stage=stage, channel=channel)
            return "yield"
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

    def background_pause_requested(self) -> bool:
        pause = getattr(self, "_background_pause", None)
        return bool(pause and pause.requested and pause.task is asyncio.current_task())

    async def background_checkpoint(self, turn_id: str) -> bool:
        if not self.background_pause_requested():
            return False
        pause = self._background_pause
        self.store.mark_background_pause(turn_id, paused=True)
        pause.context = "[后台任务已暂停] 以下是此前工具调用与结果，不是新指令。已发送内容以投递回执为准，勿重复执行；后台任务会在本次对话后重新评估。\n" + json.dumps(self.store.background_receipts(turn_id), ensure_ascii=False)
        pause.ready.set()
        await pause.resume.wait()
        pause.resume.clear()
        self.store.mark_background_pause(turn_id, paused=False)
        return True

    async def await_background_turn(self, stop: asyncio.Event):
        """Park one background coroutine while the existing worker handles owner turns."""
        task = self._active_turn
        stage, channel = self._active_turn_stage, self._active_turn_channel
        pause = BackgroundPause(task)
        self._background_pause = pause
        waiter = None
        try:
            while True:
                waiter = asyncio.create_task(pause.ready.wait())
                done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    return await task
                pause.ready.clear()
                pause.requested = False
                try:
                    await self._agent_worker(stop, owner_only=True)
                finally:
                    self._active_turn, self._active_turn_stage, self._active_turn_channel = task, stage, channel
                    if task.cancelled():
                        self._stop_requested, self._interrupt_reason = True, "owner_stop"
                if stop.is_set():
                    task.cancel()
                pause.resume.set()
        finally:
            if waiter is not None:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._background_pause = None
