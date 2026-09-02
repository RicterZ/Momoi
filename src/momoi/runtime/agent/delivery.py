import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from ...channel import ChannelMessage, normalize_channel_message, render_channel_message
from ...emotions import EMOTION_PREFIX, emotion_slug

SIMILAR_BUBBLES_THRESHOLD = 0.75


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
            if not isinstance(message, str) or not message.startswith(EMOTION_PREFIX):
                continue
            slug = emotion_slug(message)
            if slug is None:
                return "invalid_emotion_directive"
            if self.store.emotion(slug) is None:
                return "unknown_emotion_slug"
        return None
