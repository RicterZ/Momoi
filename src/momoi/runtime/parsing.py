import re
from typing import Any

from ..channel import (
    ChannelMessage,
    has_blank_line,
    normalize_channel_message,
    split_exclusive_media,
)
from ..emotions import EMOTION_PREFIX
from ..models import AgentReply
from ..storage import REFLECTION_MEMORY_KINDS


def response_text(content: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if block.get("type") == "text"
    ).strip()


def parse_bubbles(
    arguments: dict[str, Any],
) -> tuple[list[ChannelMessage] | None, str | None]:
    raw_bubbles = arguments.get("bubbles")
    if not isinstance(raw_bubbles, list) or not raw_bubbles:
        return None, "bubbles_must_be_a_non_empty_array"
    bubbles: list[ChannelMessage] = []
    for item in raw_bubbles:
        if isinstance(item, str):
            if not item.strip():
                return None, "bubbles_must_contain_non_empty_items"
            if has_blank_line(item):
                return None, "blank_lines_must_be_separate_bubbles"
            bubbles.append(item.strip())
            continue
        try:
            normalized = normalize_channel_message(item)
        except ValueError as error:
            return None, str(error)
        for message in split_exclusive_media(normalized):
            segments = message.get("segments") or []
            if (
                message.get("action") == "message"
                and len(segments) == 1
                and segments[0].get("type") == "text"
                and str(segments[0].get("data", {}).get("text", "")).startswith(
                    EMOTION_PREFIX
                )
            ):
                bubbles.append(str(segments[0]["data"]["text"]))
            else:
                bubbles.append(message)
    return bubbles, None


def parse_reply_wait_decision(
    value: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "invalid_reply_wait_decision"
    wait = value.get("wait")
    if wait is False and set(value) == {"wait"}:
        return {"wait": False}, None
    if wait is not True or set(value) != {
        "wait",
        "delay_minutes",
        "expected_information",
        "reason",
    }:
        return None, "invalid_reply_wait_decision"
    delay = value.get("delay_minutes")
    expected = value.get("expected_information")
    reason = value.get("reason")
    if (
        not isinstance(delay, int)
        or isinstance(delay, bool)
        or not 1 <= delay <= 10
        or not isinstance(expected, str)
        or not expected.strip()
        or len(expected) > 300
        or not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 500
    ):
        return None, "invalid_reply_wait_decision"
    return {
        "wait": True,
        "delay_minutes": delay,
        "expected_information": expected.strip(),
        "reason": reason.strip(),
    }, None


def parse_response(
    arguments: dict[str, Any],
    *,
    require_heartbeat: bool = False,
    allow_activity_update: bool = False,
) -> tuple[AgentReply | None, str | None]:
    if "bubbles" in arguments:
        return None, "bubbles_not_allowed_in_end_turn"
    legacy_reply_wait_fields = {
        "expects_reply",
        "reply_expectation",
        "schedule_reply_wait",
    }
    if legacy_reply_wait_fields & arguments.keys():
        return None, "legacy_reply_wait_fields_not_allowed"
    messages: list[ChannelMessage] = []
    error: str | None = None
    mood, error = parse_mood_decision(arguments.get("mood"))
    if error is not None:
        return None, error
    activity_update = None
    if allow_activity_update:
        activity_update, error = parse_activity_decision(arguments.get("activity"))
        if error is not None:
            return None, error
    elif "activity" in arguments:
        return None, "activity_update_not_allowed"
    reply_wait, error = parse_reply_wait_decision(arguments.get("reply_wait"))
    if reply_wait is None:
        return None, error
    heartbeat = arguments.get("heartbeat")
    if require_heartbeat:
        if not isinstance(heartbeat, dict):
            return None, "invalid_heartbeat_state"
        required = {
            "activity",
            "result",
            "next_check_minutes",
            "reason",
        }
        if set(heartbeat) != required:
            return None, "invalid_heartbeat_state"
        if (
            not isinstance(heartbeat["activity"], str)
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
        activity_update=activity_update,
        heartbeat=heartbeat if require_heartbeat else None,
        reply_wait=reply_wait,
    ), None


def parse_activity_decision(
    value: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "invalid_activity_decision"
    decision = value.get("decision")
    if decision == "unchanged" and set(value) == {"decision"}:
        return None, None
    if decision != "updated" or set(value) != {
        "decision",
        "text",
        "result",
    }:
        return None, "invalid_activity_decision"
    text = value.get("text")
    result = value.get("result")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 300
        or not isinstance(result, str)
        or len(result) > 2000
    ):
        return None, "invalid_activity_decision"
    return {
        "text": text.strip(),
        "result": result.strip(),
    }, None


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
        not isinstance(state, str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", state) is None
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


CONVERSATION_ACTIONS = {"close"}


def parse_conversation_actions(
    raw: object,
    open_episode_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list) or len(raw) > 32:
        return None, "invalid_conversation_action"
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "episode_id",
            "action",
            "reason",
        }:
            return None, "invalid_conversation_action"
        action = item.get("action")
        episode_id = item.get("episode_id")
        reason = item.get("reason")
        if (
            action not in CONVERSATION_ACTIONS
            or not isinstance(episode_id, str)
            or not episode_id.strip()
            or len(episode_id) > 128
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 400
        ):
            return None, "invalid_conversation_action"
        episode_id = episode_id.strip()
        if episode_id in seen:
            return None, "duplicate_conversation_action"
        seen.add(episode_id)
        if open_episode_ids is not None and episode_id not in open_episode_ids:
            return None, "unknown_open_conversation"
        actions.append(
            {
                "episode_id": episode_id,
                "action": action,
                "reason": reason.strip(),
            }
        )
    return actions, None


def parse_reflection_finish(
    arguments: dict[str, Any],
    source: str,
    owner_source: str,
    knowledge_source: str,
    open_episode_ids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(arguments, dict):
        return None, "invalid_reflection_finish"
    extra = set(arguments) - {
        "summary",
        "memories",
        "conversation_actions",
    }
    if extra or {"summary", "memories"} - set(arguments):
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
    conversation_actions, conversation_error = parse_conversation_actions(
        arguments.get("conversation_actions"), open_episode_ids
    )
    if conversation_error is not None:
        return None, conversation_error
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
    return {
        "summary": summary.strip(),
        "memories": memories,
        "conversation_actions": conversation_actions,
    }, None
