import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from ....channel import Channel
from ....observability.events import log_event
from ....models import IncomingMessage, ProviderResponse
from ...turn_support import OwnerMessagesChanged

logger = logging.getLogger("momoi.runtime.turns")


class OwnerUpdateController:
    """Owns mid-Turn owner input draining and provider interruption."""

    def __init__(
        self,
        incoming: asyncio.Queue[IncomingMessage],
        deferred: deque[IncomingMessage],
        quiet_until: dict[str, float],
        activity_changed: asyncio.Event,
        message_changed: asyncio.Event,
        channel_for: Callable[[str], Channel],
    ) -> None:
        self.incoming = incoming
        self.deferred = deferred
        self.quiet_until = quiet_until
        self.activity_changed = activity_changed
        self.message_changed = message_changed
        self.channel_for = channel_for

    def drain(
        self, current_events: list[IncomingMessage], channel_name: str
    ) -> list[IncomingMessage]:
        updates: list[IncomingMessage] = []
        for _ in range(len(self.deferred)):
            message = self.deferred.popleft()
            if self.channel_for(message.channel).name == channel_name:
                updates.append(message)
            else:
                self.deferred.append(message)
        while True:
            try:
                message = self.incoming.get_nowait()
            except asyncio.QueueEmpty:
                break
            if self.channel_for(message.channel).name == channel_name:
                updates.append(message)
            else:
                self.deferred.append(message)
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

    async def settle(
        self, current_events: list[IncomingMessage], channel_name: str
    ) -> list[IncomingMessage]:
        channel = self.channel_for(channel_name)
        loop = asyncio.get_running_loop()
        hard_deadline = loop.time() + channel.max_batch_seconds
        updates: list[IncomingMessage] = []
        while True:
            updates.extend(self.drain(current_events, channel.name))
            deadline = min(self.quiet_until.get(channel.name, 0.0), hard_deadline)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return updates
            self.activity_changed.clear()
            try:
                await asyncio.wait_for(self.activity_changed.wait(), timeout=remaining)
            except TimeoutError:
                pass

    async def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        require_tool: bool,
        current_events: list[IncomingMessage],
        channel_name: str,
        provider: Any,
    ) -> ProviderResponse:
        initial = self.drain(current_events, channel_name)
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
            provider.complete(system, messages, tools, require_tool=require_tool)
        )
        try:
            while True:
                self.message_changed.clear()
                updates = self.drain(current_events, channel_name)
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

                changed = asyncio.create_task(self.message_changed.wait())
                done, _ = await asyncio.wait(
                    {provider_task, changed},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if changed not in done:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
                updates = self.drain(current_events, channel_name)
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
