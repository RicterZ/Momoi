import logging
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from ...channel import Channel, ChannelMessage, normalize_channel_message, render_channel_message
from ...tts import AudioOutput, TTSError, TTSProvider
from ...emotions import EMOTION_PREFIX, emotion_slug
from ...observability.events import log_event
from ...models import ToolCall
from ..parsing import parse_bubbles

SIMILAR_BUBBLES_THRESHOLD = 0.75
logger = logging.getLogger("momoi.runtime.turns")


def _bubble_text(bubbles: list[ChannelMessage]) -> str:
    rendered = [
        bubble
        if isinstance(bubble, str)
        else render_channel_message(normalize_channel_message(bubble))
        for bubble in bubbles
    ]
    text = unicodedata.normalize("NFKC", "\n".join(rendered)).casefold()
    return re.sub(r"[^\w]+", "", text)


class DeliveryPolicy:
    """Validates owner-visible delivery without performing channel I/O."""

    def __init__(self, config: Any, store: Any):
        self.config = config
        self.store = store

    @staticmethod
    def similarity(
        previous: list[ChannelMessage], current: list[ChannelMessage]
    ) -> float:
        previous_text = _bubble_text(previous)
        current_text = _bubble_text(current)
        if not previous_text or not current_text:
            return 0.0
        return SequenceMatcher(
            None, previous_text, current_text, autojunk=False
        ).ratio()

    def heartbeat_contact_error(
        self, owner_event_revision: int, notification_key: str
    ) -> str | None:
        snapshot = self.store.heartbeat_conversation_snapshot()
        if int(snapshot["owner_event_revision"]) != owner_event_revision:
            return "heartbeat_superseded_by_owner_update"
        if snapshot["owner_busy"]:
            return "heartbeat_contact_unavailable"
        window = self.store.heartbeat_contact_window(
            notification_key,
            self.config.notifications,
            apply_cooldown=notification_key != "heartbeat.reply_followup",
        )
        return None if window["allowed"] else "heartbeat_contact_unavailable"

    def validate_emotions(self, messages: list[ChannelMessage]) -> str | None:
        for message in messages:
            if not isinstance(message, str):
                if EMOTION_PREFIX in render_channel_message(
                    normalize_channel_message(message)
                ):
                    return "emotion_directive_must_be_a_standalone_bubble"
                continue
            if EMOTION_PREFIX not in message:
                continue
            slug = emotion_slug(message)
            if slug is None:
                return "emotion_directive_must_be_a_standalone_bubble"
            if self.store.emotion(slug) is None:
                return "unknown_emotion_slug"
        return None


@dataclass(frozen=True)
class BubbleDeliveryResult:
    result: dict[str, object]
    bubbles: list[ChannelMessage] | None = None
    channel: str = ""


class BubbleDelivery:
    """Validate and persist one send_bubbles call without performing channel I/O."""

    def __init__(
        self,
        store: Any,
        channels: dict[str, Channel],
        policy: DeliveryPolicy,
        outbox_changed: Any,
        tts_provider: TTSProvider | None = None,
    ) -> None:
        self.store = store
        self.channels = channels
        self.policy = policy
        self.outbox_changed = outbox_changed
        self.tts_provider = tts_provider
        self.voice_audio: OrderedDict[tuple[str, str], AudioOutput] = OrderedDict()

    async def prepare_voice(self, turn_id: str, text: str, channel: str) -> dict[str, object] | None:
        revision = self.store.owner_channel_revision(channel)
        try:
            if self.tts_provider is None:
                raise TTSError("tts_not_configured")
            audio = await self.tts_provider.synthesize(text)
        except TTSError as error:
            log_event(
                logger, logging.WARNING, "voice_synthesis_failure",
                turn_id=turn_id, error_type=type(error).__name__, reason=str(error),
            )
            return {
                "ok": False, "error": "voice_synthesis_failed", "detail": str(error),
                "message": (
                    "Voice synthesis failed; no voice message was queued or delivered. "
                    "Use send_bubbles to reply in text instead. Do not repeat send_voice "
                    "for this reply; TTS retries have already been handled."
                ),
            }
        if self.store.owner_channel_revision(channel) != revision:
            return {"ok": False, "error": "superseded_by_owner_update"}
        self.voice_audio[(turn_id, text)] = audio
        self.voice_audio.move_to_end((turn_id, text))
        # Bound memory for staged or subsequently cancelled messages. Evicted
        # audio can be synthesized again by the durable outbox, as on restart.
        while len(self.voice_audio) > 8:
            self.voice_audio.popitem(last=False)
        return None

    async def send_voice(
        self, text: str, *, tool_call_id: str, turn_id: str,
        delivery_channel: Channel, response_required: bool,
        heartbeat_turn: bool, reply_followup_turn: bool,
        heartbeat_owner_event_revision: int | None,
        heartbeat_notification_key: str,
    ) -> BubbleDeliveryResult:
        """Speak a complete string; only the channel payload is audio."""
        def failure(error: str) -> BubbleDeliveryResult:
            return BubbleDeliveryResult({"ok": False, "error": error})

        if not isinstance(text, str) or not text.strip():
            return failure("invalid_voice_text")
        if EMOTION_PREFIX in text:
            return failure("voice_cannot_include_emotion")
        if not tool_call_id:
            return failure("missing_tool_call_id")
        if not (response_required or heartbeat_turn):
            return failure("tool_not_allowed")
        if not callable(getattr(delivery_channel, "send_voice", None)):
            return failure("voice_not_supported")
        if self.tts_provider is None:
            return failure("tts_not_configured")

        def contact_error() -> str | None:
            if (heartbeat_turn or reply_followup_turn) and heartbeat_owner_event_revision is not None:
                return self.policy.heartbeat_contact_error(
                    heartbeat_owner_event_revision, heartbeat_notification_key,
                )
            return None

        error = contact_error()
        if error:
            return failure(error)
        existing = self.store.progress_delivery(turn_id, tool_call_id)
        if existing is not None:
            if (existing["text"] != text or existing["target_channel"] != delivery_channel.name
                    or existing["kind"] != "voice"):
                return failure("tool_call_id_conflict")
            if existing["state"] in {"failed", "superseded"}:
                return BubbleDeliveryResult({
                    "ok": False, "error": "voice_delivery_failed",
                    "detail": str(existing["last_error"] or existing["state"]),
                    "message": "This voice message failed or was cancelled; do not report it as sent.",
                })
        else:
            synthesis_error = await self.prepare_voice(turn_id, text, delivery_channel.name)
            if synthesis_error is not None:
                return BubbleDeliveryResult(synthesis_error)
            error = contact_error()
            if error:
                self.voice_audio.pop((turn_id, text), None)
                return failure(error)
            self.store.queue_progress(
                turn_id, tool_call_id, [text], delivery_channel.name, voice=True,
            )
            self.outbox_changed.set()
        return BubbleDeliveryResult(
            {"ok": True, "state": "committed", "channel": delivery_channel.name, "bubbles": 1},
            bubbles=[text], channel=delivery_channel.name,
        )

    async def dispatch_voice(self, call: ToolCall, **context: Any) -> BubbleDeliveryResult:
        if set(call.arguments) != {"text"}:
            return BubbleDeliveryResult({"ok": False, "error": "invalid_voice_arguments"})
        text = call.arguments["text"]
        if (isinstance(text, str) and context.get("previous_tool_name") in {"send_bubbles", "send_voice"}
                and context.get("previous_bubbles") is not None
                and context.get("previous_channel") == context["delivery_channel"].name
                and self.policy.similarity(context["previous_bubbles"], [text]) >= SIMILAR_BUBBLES_THRESHOLD):
            return BubbleDeliveryResult({"ok": False, "error": "similar_bubbles_already_sent"})
        return await self.send_voice(
            call.arguments["text"], tool_call_id=call.id,
            **{key: context[key] for key in (
                "turn_id", "delivery_channel", "response_required", "heartbeat_turn",
                "reply_followup_turn", "heartbeat_owner_event_revision", "heartbeat_notification_key",
            )},
        )

    def dispatch(
        self,
        call: ToolCall,
        *,
        turn_id: str,
        stage: str,
        round_number: int,
        delivery_channel: Channel,
        response_required: bool,
        heartbeat_turn: bool,
        reply_followup_turn: bool,
        heartbeat_owner_event_revision: int | None,
        heartbeat_notification_key: str,
        previous_tool_name: str | None,
        previous_bubbles: list[ChannelMessage] | None,
        previous_channel: str,
    ) -> BubbleDeliveryResult:
        if not call.id:
            return BubbleDeliveryResult(
                {"ok": False, "error": "missing_tool_call_id"}
            )
        bubbles, error = parse_bubbles(call.arguments)
        if bubbles is not None:
            error = self.policy.validate_emotions(bubbles)
            if error is not None:
                bubbles = None
        if not (response_required or heartbeat_turn):
            return BubbleDeliveryResult({"ok": False, "error": "tool_not_allowed"})
        if bubbles is None:
            return BubbleDeliveryResult({"ok": False, "error": error})
        check_contact = (
            (heartbeat_turn or reply_followup_turn)
            and heartbeat_owner_event_revision is not None
        )
        contact_error = (
            self.policy.heartbeat_contact_error(
                heartbeat_owner_event_revision,
                heartbeat_notification_key,
            )
            if check_contact
            else None
        )
        if contact_error is not None:
            return BubbleDeliveryResult({"ok": False, "error": contact_error})
        target = self.channels.get(
            str(call.arguments.get("channel") or delivery_channel.name)
        )
        if target is None:
            return BubbleDeliveryResult({"ok": False, "error": "invalid_channel"})
        similarity = (
            self.policy.similarity(previous_bubbles, bubbles)
            if previous_tool_name in {"send_bubbles", "send_voice"}
            and previous_bubbles is not None
            and previous_channel == target.name
            else 0.0
        )
        if similarity >= SIMILAR_BUBBLES_THRESHOLD:
            log_event(
                logger,
                logging.WARNING,
                "similar_send_bubbles_skipped",
                stage=stage,
                turn_id=turn_id,
                round=round_number,
                channel=target.name,
                tool_call_id=call.id,
                similarity=round(similarity, 3),
                threshold=SIMILAR_BUBBLES_THRESHOLD,
            )
            return BubbleDeliveryResult(
                {
                    "ok": False,
                    "error": "similar_bubbles_already_sent",
                    "message": (
                        "A very similar set of bubbles was already sent successfully. "
                        "Do not repeat it; continue the work or end the Turn."
                    ),
                }
            )
        self.store.queue_progress(turn_id, call.id, bubbles, target.name)
        self.outbox_changed.set()
        return BubbleDeliveryResult(
            {
                "ok": True,
                "state": "committed",
                "channel": target.name,
                "bubbles": len(bubbles),
            },
            bubbles=bubbles,
            channel=target.name,
        )
