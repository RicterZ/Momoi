import re
from typing import Any

from ..channel import ChannelMessage, normalize_channel_message
from ..emotions import EMOTION_PREFIX
from ..models import AgentReply
from ..storage import MOOD_STATES, REFLECTION_MEMORY_KINDS


def parse_messages(
    arguments: dict[str, Any], *, allow_empty: bool = False
) -> tuple[list[ChannelMessage] | None, str | None]:
    raw_messages = arguments.get("messages")
    if not isinstance(raw_messages, list) or (not raw_messages and not allow_empty):
        return None, "messages_must_be_a_non_empty_array"
    messages: list[ChannelMessage] = []
    for item in raw_messages:
        if isinstance(item, str):
            text = item.strip()
            if not text:
                return None, "messages_must_contain_non_empty_items"
            messages.append(text)
            continue
        try:
            message = normalize_channel_message(item)
        except ValueError as error:
            return None, str(error)
        segments = message.get("segments") or []
        if (
            message.get("action") == "message"
            and len(segments) == 1
            and segments[0].get("type") == "text"
            and str(segments[0].get("data", {}).get("text", "")).startswith(
                EMOTION_PREFIX
            )
        ):
            messages.append(str(segments[0]["data"]["text"]))
        else:
            messages.append(message)
    return messages, None


def parse_reply_expectation(
    arguments: dict[str, Any], messages: list[ChannelMessage]
) -> tuple[tuple[bool, str] | None, str | None]:
    expects_reply = arguments.get("expects_reply")
    expectation = arguments.get("reply_expectation")
    if not isinstance(expects_reply, bool):
        return None, "invalid_expects_reply"
    if not isinstance(expectation, str) or len(expectation) > 300:
        return None, "invalid_reply_expectation"
    expectation = expectation.strip()
    if expects_reply and not expectation:
        return None, "invalid_reply_expectation"
    if not expects_reply and expectation:
        return None, "invalid_reply_expectation"
    return (expects_reply, expectation), None


def parse_response(
    arguments: dict[str, Any],
) -> tuple[AgentReply | None, str | None]:
    messages, error = parse_messages(arguments, allow_empty=True)
    if messages is None:
        return None, error
    mood, error = parse_mood_decision(arguments.get("mood"))
    if error is not None:
        return None, error
    reply_expectation, error = parse_reply_expectation(arguments, messages)
    if reply_expectation is None:
        return None, error
    expects_reply, expectation = reply_expectation
    return AgentReply(
        messages,
        mood_transition=mood,
        expects_reply=expects_reply,
        reply_expectation=expectation,
    ), None


def parse_mood_decision(
    value: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "invalid_mood_decision"
    action = value.get("action")
    if action == "keep" and set(value) == {"action"}:
        return None, None
    if action != "transition":
        return None, "invalid_mood_decision"
    transition = {key: item for key, item in value.items() if key != "action"}
    mood, error = parse_mood_transition(transition)
    return mood, "invalid_mood_decision" if error else None


def parse_mood_transition(
    value: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict) or set(value) != {
        "state",
        "intensity",
        "cause",
        "duration_minutes",
    }:
        return None, "invalid_mood_transition"
    state = value.get("state")
    cause = value.get("cause")
    intensity = value.get("intensity")
    duration = value.get("duration_minutes")
    if (
        state not in MOOD_STATES
        or not isinstance(cause, str)
        or not cause.strip()
        or len(cause) > 300
    ):
        return None, "invalid_mood_transition"
    if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
        return None, "invalid_mood_transition"
    if not 0 <= float(intensity) <= 1:
        return None, "invalid_mood_transition"
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 5 <= duration <= 1440
    ):
        return None, "invalid_mood_transition"
    return {
        "state": state,
        "intensity": float(intensity),
        "cause": cause.strip()[:300],
        "duration_minutes": duration,
    }, None


def parse_reflection_finish(
    arguments: dict[str, Any],
    source: str,
    owner_source: str,
    knowledge_source: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(arguments, dict) or set(arguments) != {"summary", "memories"}:
        return None, "invalid_reflection_finish"
    summary = arguments.get("summary")
    raw_memories = arguments.get("memories")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > 6000
        or not isinstance(raw_memories, list)
        or len(raw_memories) > 12
    ):
        return None, "invalid_reflection_finish"
    memories: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    interaction_practices = 0
    for item in raw_memories:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "key",
            "content",
            "evidence",
            "confidence",
        }:
            return None, "invalid_reflection_memory"
        kind = item.get("kind")
        key = item.get("key")
        content = item.get("content")
        evidence = item.get("evidence")
        confidence = item.get("confidence")
        if (
            kind not in REFLECTION_MEMORY_KINDS
            or not isinstance(key, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,199}", key)
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 1000
            or not isinstance(evidence, str)
            or not evidence.strip()
            or len(evidence) > 500
            or evidence not in source
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            return None, "invalid_reflection_memory"
        if (
            kind in {"owner_profile", "owner_preference"}
            and evidence not in owner_source
        ):
            return None, "owner_reflection_requires_owner_evidence"
        if kind == "world_knowledge" and evidence not in knowledge_source:
            return None, "world_reflection_requires_observed_evidence"
        if kind == "practice" and key.startswith("interaction."):
            if evidence not in owner_source:
                return None, "interaction_practice_requires_owner_evidence"
            interaction_practices += 1
            if interaction_practices > 1:
                return None, "too_many_interaction_practices"
        identity = (kind, key)
        if identity in seen:
            return None, "duplicate_reflection_memory"
        seen.add(identity)
        memories.append(
            {
                "kind": kind,
                "key": key,
                "content": content.strip(),
                "evidence": evidence.strip(),
                "confidence": float(confidence),
            }
        )
    return {"summary": summary.strip(), "memories": memories}, None
