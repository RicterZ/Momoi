import asyncio
import logging
import random
from time import monotonic

from ...channel import AmbiguousSend, NotConnected, SendRejected
from ...observability.events import TRACE, log_event
from ...observability.values import safe_preview
from ...policies import DaemonPolicy
from ...tts import TTSError

logger = logging.getLogger("momoi.runtime.daemon")
_DEFAULT_DAEMON_POLICY = DaemonPolicy()


def message_gap_bounds(
    text: str, policy: DaemonPolicy = _DEFAULT_DAEMON_POLICY
) -> tuple[float, float]:
    ratio = min(
        1.0,
        max(
            0.0,
            (len(text.strip()) - policy.message_gap_min_chars)
            / (policy.message_gap_saturation_chars - policy.message_gap_min_chars),
        ),
    )
    lower = policy.message_gap_min_seconds + 2 * ratio
    upper = lower + 1
    return lower, min(policy.message_gap_max_seconds, upper)


class OutboxWorker:
    async def _outbox_worker(self, stop: asyncio.Event) -> None:
        previous_delivery: tuple[str, str] | None = None
        while not stop.is_set():
            self.outbox_changed.clear()
            rows = self.store.due_outbox()
            if not rows:
                try:
                    await asyncio.wait_for(self.outbox_changed.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            for row in rows:
                try:
                    channel = self._channel_for(row.channel)
                except ValueError:
                    log_event(
                        logger,
                        logging.ERROR,
                        "outbox_failure",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=row.channel,
                        outbox_id=row.id,
                        reason="channel_not_configured",
                    )
                    self.store.mark_not_dispatched(row.id, "ChannelNotConfigured")
                    continue
                delivery = (row.channel, row.turn_id)
                if delivery == previous_delivery:
                    delay = random.uniform(
                        *message_gap_bounds(row.text, self.daemon_policy)
                    )
                    log_event(
                        logger,
                        TRACE,
                        "outbox_delay",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        delay_ms=int(delay * 1000),
                    )
                    await asyncio.sleep(delay)
                audio = None
                if row.kind == "voice":
                    try:
                        if not callable(getattr(channel, "send_voice", None)):
                            raise TTSError("voice_not_supported")
                        provider = self.bubble_delivery.tts_provider
                        if provider is None:
                            raise TTSError("tts_not_configured")
                        audio = await provider.synthesize(row.text)
                    except TTSError as error:
                        if self.store.mark_sending(row.id):
                            self.store.mark_failed(row.id, str(error))
                        log_event(
                            logger, logging.WARNING, "voice_synthesis_failure",
                            stage="delivery", turn_id=row.turn_id, channel=channel.name,
                            outbox_id=row.id, error_type=type(error).__name__,
                        )
                        continue
                # Synthesis may take time; respect messages cancelled during it.
                if stop.is_set():
                    return
                if not self.store.mark_sending(row.id):
                    continue
                attempt = row.attempts + 1
                send_started = monotonic()
                try:
                    log_event(
                        logger,
                        TRACE,
                        "outbox_send",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        kind=row.kind,
                        content=safe_preview(row.text, 500),
                    )
                    if row.kind == "voice":
                        await channel.send_voice(audio)
                    else:
                        await channel.send_message(
                            row.payload
                            or {
                                "action": "message",
                                "segments": [{"type": "text", "data": {"text": row.text}}],
                            }
                        )
                except NotConnected as error:
                    self.store.mark_not_dispatched(row.id, type(error).__name__)
                    log_event(
                        logger,
                        logging.WARNING,
                        "outbox_retry",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        error_type=type(error).__name__,
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
                    continue
                except AmbiguousSend as error:
                    self.store.mark_ambiguous(row.id, attempt, type(error).__name__)
                    log_event(
                        logger,
                        logging.WARNING,
                        "outbox_ambiguous",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        error_type=type(error).__name__,
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
                except SendRejected as error:
                    self.store.mark_failed(row.id, str(error))
                    log_event(
                        logger,
                        logging.WARNING,
                        "outbox_failure",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        error_type=type(error).__name__,
                        reason=safe_preview(str(error), 300),
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
                else:
                    reply_waiting = self.store.mark_sent(row.id)
                    if reply_waiting:
                        self.agenda_changed.set()
                    previous_delivery = delivery
                    log_event(
                        logger,
                        logging.DEBUG,
                        "outbox_sent",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
