import re
from typing import Any

from ..channel import ChannelMessage, has_blank_line, normalize_channel_message
from ..emotions import EMOTION_PREFIX
from ..models import AgentReply
from ..storage import REFLECTION_MEMORY_KINDS


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
    arguments: dict[str, Any]
) -> tuple[tuple[bool, str] | None, str | None]:
    expectation = arguments.get("reply_expectation")
    if not isinstance(expectation, str) or len(expectation) > 300:
        return None, "invalid_reply_expectation"
    expectation = expectation.strip()
    return (bool(expectation), expectation), None


def parse_response(
    arguments: dict[str, Any],
    *,
    require_heartbeat: bool = False,
    require_reply_wait: bool = False,
) -> tuple[AgentReply | None, str | None]:
    if require_heartbeat and require_reply_wait:
        return None, "conflicting_terminal_state"
    if require_reply_wait and set(arguments) != {"reply_wait", "mood"}:
        return None, "invalid_reply_wait_state"
    if "messages" in arguments:
        return None, "messages_not_allowed_in_respond"
    messages: list[ChannelMessage] = []
    error: str | None = None
    mood, error = parse_mood_decision(arguments.get("mood"))
    if error is not None:
        return None, error
    if require_reply_wait:
        expects_reply, expectation = False, ""
    else:
        reply_expectation, error = parse_reply_expectation(arguments)
        if reply_expectation is None:
            return None, error
        expects_reply, expectation = reply_expectation
    heartbeat = arguments.get("heartbeat")
    reply_wait = arguments.get("reply_wait")
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
    if require_reply_wait:
        if (
            not isinstance(reply_wait, dict)
            or set(reply_wait) != {"continue_waiting", "reason"}
            or not isinstance(reply_wait["continue_waiting"], bool)
            or not isinstance(reply_wait["reason"], str)
            or not reply_wait["reason"].strip()
            or len(reply_wait["reason"]) > 500
        ):
            return None, "invalid_reply_wait_state"
        reply_wait = {
            **reply_wait,
            "reason": reply_wait["reason"].strip(),
        }
    elif reply_wait is not None:
        return None, "reply_wait_state_not_allowed"
    return AgentReply(
        messages,
        mood_update=mood,
        expects_reply=expects_reply,
        reply_expectation=expectation,
        heartbeat=heartbeat if require_heartbeat else None,
        reply_wait=reply_wait if require_reply_wait else None,
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


ALWAYS_MEMORY_ACTIONS = {
    "demote_recent",
    "demote_recall",
    "merge",
    "forget",
}
CONVERSATION_ACTIONS = {"close"}


def parse_always_memory_actions(
    raw: object,
    always_memory_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list) or len(raw) > 32:
        return None, "invalid_always_memory_action"
    actions: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None, "invalid_always_memory_action"
        keys = set(item)
        if keys != {"memory_id", "action", "reason"} and keys != {
            "memory_id",
            "action",
            "reason",
            "merge_into_id",
            "content",
        }:
            return None, "invalid_always_memory_action"
        action = item.get("action")
        memory_id = item.get("memory_id")
        reason = item.get("reason")
        merge_into_id = item.get("merge_into_id")
        content = item.get("content")
        if (
            action not in ALWAYS_MEMORY_ACTIONS
            or isinstance(memory_id, bool)
            or not isinstance(memory_id, int)
            or memory_id < 1
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 400
        ):
            return None, "invalid_always_memory_action"
        if action == "merge":
            if (
                isinstance(merge_into_id, bool)
                or not isinstance(merge_into_id, int)
                or merge_into_id < 1
                or merge_into_id == memory_id
                or not isinstance(content, str)
                or not content.strip()
                or len(content) > 2000
            ):
                return None, "invalid_always_memory_merge"
        elif "content" in item or merge_into_id is not None:
            return None, "invalid_always_memory_action"
        if memory_id in seen:
            return None, "duplicate_always_memory_action"
        seen.add(memory_id)
        if always_memory_ids is not None and memory_id not in always_memory_ids:
            return None, "unknown_always_memory"
        if (
            action == "merge"
            and always_memory_ids is not None
            and merge_into_id not in always_memory_ids
        ):
            return None, "unknown_always_memory"
        parsed = {
            "memory_id": memory_id,
            "action": action,
            "reason": reason.strip(),
        }
        if action == "merge":
            parsed["merge_into_id"] = merge_into_id
            parsed["content"] = content.strip()
        actions.append(parsed)
    forget_ids = {item["memory_id"] for item in actions if item["action"] == "forget"}
    merge_sources = {item["memory_id"] for item in actions if item["action"] == "merge"}
    for item in actions:
        if item["action"] != "merge":
            continue
        target = item["merge_into_id"]
        if target in forget_ids or target in merge_sources:
            return None, "invalid_always_memory_merge"
    return actions, None


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
    always_memory_ids: set[int] | None = None,
    open_episode_ids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(arguments, dict):
        return None, "invalid_reflection_finish"
    extra = set(arguments) - {
        "summary",
        "memories",
        "always_memory_actions",
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
    always_memory_actions, action_error = parse_always_memory_actions(
        arguments.get("always_memory_actions"), always_memory_ids
    )
    if action_error is not None:
        return None, action_error
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
        "always_memory_actions": always_memory_actions,
        "conversation_actions": conversation_actions,
    }, None
