import re
from typing import Any

from ..channel import ChannelMessage, has_blank_line, normalize_channel_message
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
            if not item.strip():
                return None, "messages_must_contain_non_empty_items"
            if has_blank_line(item):
                return None, "blank_lines_must_be_separate_messages"
            messages.append(item.strip())
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
    arguments: dict[str, Any], *, require_heartbeat: bool = False
) -> tuple[AgentReply | None, str | None]:
    messages, error = parse_messages(arguments, allow_empty=True)
    if messages is None:
        return None, error
    if require_heartbeat and len(messages) > 3:
        return None, "invalid_heartbeat_messages"
    mood, error = parse_mood_decision(arguments.get("mood"))
    if error is not None:
        return None, error
    reply_expectation, error = parse_reply_expectation(arguments, messages)
    if reply_expectation is None:
        return None, error
    expects_reply, expectation = reply_expectation
    heartbeat = arguments.get("heartbeat")
    if require_heartbeat:
        if not isinstance(heartbeat, dict):
            return None, "invalid_heartbeat_state"
        required = {
            "continue_waiting_for_reply",
            "activity",
            "result",
            "next_check_minutes",
            "reason",
        }
        if set(heartbeat) != required:
            return None, "invalid_heartbeat_state"
        if (
            not isinstance(heartbeat["continue_waiting_for_reply"], bool)
            or not isinstance(heartbeat["activity"], str)
            or not heartbeat["activity"].strip()
            or len(heartbeat["activity"]) > 300
            or not isinstance(heartbeat["result"], str)
            or len(heartbeat["result"]) > 2000
            or not isinstance(heartbeat["next_check_minutes"], int)
            or isinstance(heartbeat["next_check_minutes"], bool)
            or not isinstance(heartbeat["reason"], str)
            or not heartbeat["reason"].strip()
            or len(heartbeat["reason"]) > 500
        ):
            return None, "invalid_heartbeat_state"
        heartbeat = {
            **heartbeat,
            "activity": heartbeat["activity"].strip(),
            "result": heartbeat["result"].strip(),
            "reason": heartbeat["reason"].strip(),
        }
    elif heartbeat is not None:
        return None, "heartbeat_state_not_allowed"
    return AgentReply(
        messages,
        mood_update=mood,
        expects_reply=expects_reply,
        reply_expectation=expectation,
        heartbeat=heartbeat if require_heartbeat else None,
    ), None


def parse_mood_decision(
    value: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "invalid_mood_decision"
    decision = value.get("decision")
    if decision == "unchanged" and set(value) == {"decision"}:
        return None, None
    if decision != "updated":
        return None, "invalid_mood_decision"
    update = {key: item for key, item in value.items() if key != "decision"}
    mood, error = parse_mood_update(update)
    return mood, "invalid_mood_decision" if error else None


def parse_mood_update(
    value: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict) or set(value) != {"state", "intensity", "cause"}:
        return None, "invalid_mood_update"
    state = value.get("state")
    cause = value.get("cause")
    intensity = value.get("intensity")
    if (
        state not in MOOD_STATES
        or not isinstance(cause, str)
        or not cause.strip()
        or len(cause) > 300
    ):
        return None, "invalid_mood_update"
    if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
        return None, "invalid_mood_update"
    if not 0 <= float(intensity) <= 1:
        return None, "invalid_mood_update"
    return {
        "state": state,
        "intensity": float(intensity),
        "cause": cause.strip()[:300],
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
