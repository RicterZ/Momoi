import asyncio
import logging

from ...observability.events import TRACE, log_event
from ...observability.values import safe_preview
from ...models import IncomingMessage, OwnerInputStatus
from ..jobs import AutonomousJob
from ..workflows.memory_maintenance import MEMORY_MAINTENANCE_RUN_VERSION

logger = logging.getLogger("momoi.runtime.daemon")


class CommandRouter:
    def _touch_owner_activity(
        self, channel_name: str, *, quiet_extension: float | None = None
    ) -> None:
        channel = self._channel_for(channel_name)
        now = asyncio.get_running_loop().time()
        self._last_owner_activity_at = now
        extension = (
            channel.quiet_seconds if quiet_extension is None else quiet_extension
        )
        self._owner_quiet_until[channel.name] = max(
            self._owner_quiet_until.get(channel.name, 0.0),
            now + max(0.0, extension),
        )
        self._owner_activity_changed.set()

    async def _receive(self, event: IncomingMessage | OwnerInputStatus) -> None:
        if isinstance(event, OwnerInputStatus):
            channel = self._channel_for(event.channel)
            self._touch_owner_activity(
                event.channel,
                quiet_extension=channel.quiet_seconds / 2,
            )
            log_event(logger, TRACE, "owner_input_status", channel=event.channel)
            return
        message = event
        log_event(
            logger,
            logging.INFO,
            "owner_message_received",
            channel=message.channel,
            event_id=message.event_id,
            content=safe_preview(message.text, 500),
        )
        if message.text.strip() == "/stop":
            stop_channel = self._channel_for(message.channel).name
            cancelled_outbox = self.store.cancel_pending_outbox(
                stop_channel, "owner_stop"
            )
            if cancelled_outbox:
                self.outbox_changed.set()
            active = self._active_turn
            if active is not None and not active.done():
                self._stop_requested = True
                active.cancel()
            if self.store.add_event(message):
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_accepted",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="stop",
                    cancelled_outbox=cancelled_outbox,
                )
                await self.incoming.put(message)
            return
        if message.text.strip() == "/heartbeat":
            if self.store.claim_manual_heartbeat():
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_accepted",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="heartbeat",
                )
                self._manual_heartbeat_channel = message.channel
                await self.autonomous.put(AutonomousJob.heartbeat())
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_ignored",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="heartbeat",
                    reason="heartbeat_already_active",
                )
            return
        if message.text.strip() == "/tidy":
            turn_id = self.store.pending_memory_maintenance_turn()
            if turn_id is None:
                turn_id = self._turn_id(
                    "memory-maintenance",
                    MEMORY_MAINTENANCE_RUN_VERSION,
                    "manual",
                    message.event_id,
                )
                self.store.queue_memory_maintenance_turn(
                    turn_id, f"manual:{message.event_id}"
                )
            annealing = self._active_annealing
            if annealing is not None and not annealing.done():
                annealing.cancel("memory_maintenance")
            self._enqueue_memory_maintenance(turn_id)
            log_event(
                logger,
                logging.INFO,
                "owner_command_accepted",
                channel=message.channel,
                event_id=message.event_id,
                command="tidy",
                turn_id=turn_id,
            )
            return
        if message.text.strip() == "/reflect":
            reflection = self.store.claim_manual_reflection()
            if reflection is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_accepted",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="reflect",
                    local_date=reflection["local_date"],
                )
                await self.autonomous.put(
                    AutonomousJob.reflection(str(reflection["local_date"]))
                )
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_ignored",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="reflect",
                    reason="reflection_already_active",
                )
            return
        if self.store.add_event(message):
            self.store.cancel_pending_outbox(
                self._channel_for(message.channel).name,
                "owner_message_superseded_outbox",
            )
            self.outbox_changed.set()
            await self.incoming.put(message)
            annealing = self._active_annealing
            if annealing is not None and not annealing.done():
                annealing.cancel("owner_update")
            if self.config.episode_annealing.enabled:
                self.episode_annealing_requested.set()
            self._owner_message_changed.set()
            self._touch_owner_activity(message.channel)
