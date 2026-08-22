import json
import re
from collections.abc import Mapping, Sequence


def _text(value: object) -> str:
    return str(value or "").strip()


def _lines(values: object) -> str:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return "none"
    items = [_text(value) for value in values if _text(value)]
    return ", ".join(items) if items else "none"


def _speaker(message: Mapping[str, object]) -> str:
    role = _text(message.get("role")).lower()
    if role == "user":
        return "OWNER"
    if role == "assistant":
        delivery = _text(message.get("delivery_state")) or "unknown"
        return f"MOMOI delivery={delivery}"
    if role == "event":
        return "EVENT"
    return role.upper() or "UNKNOWN"


def _render_conversation_turn(
    turn: Mapping[str, object],
    number: int,
    *,
    include_episode: bool,
) -> str:
    lines = [
        f"Turn {number}",
        f"  turn id: {_text(turn.get('turn_id'))}",
    ]
    if include_episode:
        lines.extend(
            [
                f"  attached episode id: {_text(turn.get('episode_id'))}",
                f"  attached episode title: {_text(turn.get('episode_title'))}",
            ]
        )
    messages = turn.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        messages = []
    lines.append("  messages:")
    rendered_messages = 0
    for message_number, message in enumerate(messages, 1):
        if not isinstance(message, Mapping):
            continue
        rendered_messages += 1
        content = str(message.get("content") or "")
        timestamp = _text(message.get("timestamp")) or "unknown"
        lines.append(
            f"    {message_number}. [{_speaker(message)} timestamp={timestamp}]"
        )
        lines.extend(f"       {line}" for line in content.splitlines() or [""])
    if not rendered_messages:
        lines.append("    none")
    return "\n".join(lines)


def render_episode_consolidation_request(candidate: Mapping[str, object]) -> str:
    """Render the exact human-readable consolidation input sent to the LLM."""

    pending = candidate.get("turns")
    pending_turns = (
        pending
        if isinstance(pending, Sequence) and not isinstance(pending, (str, bytes))
        else []
    )
    context = candidate.get("context_turns")
    context_turns = (
        context
        if isinstance(context, Sequence) and not isinstance(context, (str, bytes))
        else []
    )
    episodes = candidate.get("candidate_episodes")
    candidate_episodes = (
        episodes
        if isinstance(episodes, Sequence) and not isinstance(episodes, (str, bytes))
        else []
    )

    pending_text = "\n\n".join(
        _render_conversation_turn(turn, number, include_episode=False)
        for number, turn in enumerate(pending_turns, 1)
        if isinstance(turn, Mapping)
    )
    context_text = "\n\n".join(
        _render_conversation_turn(turn, number, include_episode=True)
        for number, turn in enumerate(context_turns, 1)
        if isinstance(turn, Mapping)
    )
    episode_blocks: list[str] = []
    for number, episode in enumerate(candidate_episodes, 1):
        if not isinstance(episode, Mapping):
            continue
        lines = [
            f"Episode {number}",
            f"  id: {_text(episode.get('id'))}",
            f"  title: {_text(episode.get('title'))}",
            f"  status: {_text(episode.get('status')) or 'unknown'}",
        ]
        summary = _text(episode.get("narrative_summary"))
        if summary:
            lines.append(f"  summary: {summary}")
        lines.extend(
            [
                f"  topics: {_lines(episode.get('topics'))}",
                f"  entities: {_lines(episode.get('entities'))}",
                f"  open loops: {_lines(episode.get('open_loops'))}",
            ]
        )
        episode_blocks.append("\n".join(lines))

    return (
        "<pending_turns>\n"
        + (pending_text or "none")
        + "\n</pending_turns>\n\n<later_context_turns>\n"
        + (context_text or "none")
        + "\n</later_context_turns>\n\n<candidate_episodes>\n"
        + ("\n\n".join(episode_blocks) or "none")
        + "\n</candidate_episodes>"
    )


def _exact_content_block(label: str, content: object) -> list[str]:
    raw = str(content or "")
    tag = f"exact_{label.lower()}"
    return [
        f"<{tag}>",
        raw,
        f"</{tag}>",
    ]


def render_episode_annealing_request(
    episode: Mapping[str, object],
    messages: Sequence[Mapping[str, object]],
    relevant_memories: Sequence[Mapping[str, object]] = (),
) -> str:
    """Render raw evidence without escaping or altering quoteable source text."""

    claim_blocks: list[str] = []
    claims = episode.get("working_summary_claims")
    if isinstance(claims, Sequence) and not isinstance(claims, (str, bytes)):
        for number, claim in enumerate(claims, 1):
            if not isinstance(claim, Mapping):
                continue
            claim_lines = [
                f"Claim {number} [message_id={_text(claim.get('message_id'))} | "
                f"turn_id={_text(claim.get('turn_id'))} | "
                f"ordinal={_text(claim.get('ordinal'))} | "
                f"source={_speaker(claim)}]"
            ]
            claim_lines.extend(_exact_content_block("QUOTE", claim.get("quote")))
            claim_blocks.append("\n".join(claim_lines))

    message_blocks: list[str] = []
    for number, message in enumerate(messages, 1):
        message_lines = [
            f"Message {number} [message_id={_text(message.get('id'))} | "
            f"turn_id={_text(message.get('turn_id'))} | "
            f"ordinal={_text(message.get('ordinal'))} | "
            f"source={_speaker(message)} | "
            f"timestamp={_text(message.get('timestamp')) or 'unknown'}]"
        ]
        message_lines.extend(_exact_content_block("CONTENT", message.get("content")))
        message_blocks.append("\n".join(message_lines))

    memory_blocks: list[str] = []
    for number, memory in enumerate(relevant_memories, 1):
        memory_lines = [
            f"Memory {number} [memory_id={_text(memory.get('id'))} | "
            f"kind={_text(memory.get('kind'))} | "
            f"key={_text(memory.get('key'))} | "
            f"activation={_text(memory.get('activation'))} | "
            f"importance={_text(memory.get('importance'))}]"
        ]
        memory_lines.extend(
            _exact_content_block("MEMORY_CONTENT", memory.get("content"))
        )
        memory_blocks.append("\n".join(memory_lines))

    return (
        "<episode>\n"
        f"id: {_text(episode.get('id'))}\n"
        f"title: {_text(episode.get('title'))}\n"
        "</episode>\n\n<previous_verified_claims>\n"
        + ("\n\n".join(claim_blocks) or "none")
        + "\n</previous_verified_claims>\n\n<new_messages>\n"
        + ("\n\n".join(message_blocks) or "none")
        + "\n</new_messages>\n\n<relevant_memories>\n"
        + ("\n\n".join(memory_blocks) or "none")
        + "\n</relevant_memories>"
    )


def _parse_memory_actions(
    value: object, relevant_memory_ids: set[int] | None
) -> list[dict[str, object]]:
    # Target membership is deliberately rechecked by storage so one stale or
    # hallucinated action cannot discard an otherwise valid Episode summary.
    _ = relevant_memory_ids
    if not isinstance(value, list) or len(value) > 12:
        raise RuntimeError("episode summary provider returned invalid memory actions")
    actions: list[dict[str, object]] = []
    remembered_keys: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("episode summary provider returned invalid memory action")
        action = item.get("action")
        if action == "forget":
            if set(item) != {
                "action",
                "target_memory_id",
                "evidence_message_id",
                "evidence",
            }:
                raise RuntimeError(
                    "episode summary provider returned invalid memory action"
                )
        elif action in {"remember", "update"}:
            required = {
                "action",
                "target_memory_id",
                "content",
                "activation",
                "ttl_hours",
                "importance",
                "evidence_message_id",
                "evidence",
            }
            if action == "remember":
                required |= {"kind", "key"}
            if set(item) != required:
                raise RuntimeError(
                    "episode summary provider returned invalid memory action"
                )
            activation = item.get("activation")
            ttl_hours = item.get("ttl_hours")
            importance = item.get("importance")
            if (
                activation not in {"always", "recent", "recall"}
                or isinstance(ttl_hours, bool)
                or not isinstance(ttl_hours, (int, float))
                or float(ttl_hours) < 0
                or isinstance(importance, bool)
                or not isinstance(importance, (int, float))
                or not 0 <= float(importance) <= 1
                or not isinstance(item.get("content"), str)
                or not str(item["content"]).strip()
                or len(str(item["content"])) > 2000
            ):
                raise RuntimeError(
                    "episode summary provider returned invalid memory action"
                )
            if action == "remember":
                kind = item.get("kind")
                key = item.get("key")
                if (
                    kind
                    not in {
                        "profile",
                        "preference",
                        "relationship",
                        "shared",
                        "episodic",
                        "routine",
                    }
                    or not isinstance(key, str)
                    or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,199}", key) is None
                ):
                    raise RuntimeError(
                        "episode summary provider returned invalid memory action"
                    )
                identity = (str(kind), key)
                if identity in remembered_keys:
                    raise RuntimeError(
                        "episode summary provider returned duplicate memory action"
                    )
                remembered_keys.add(identity)
        else:
            raise RuntimeError("episode summary provider returned invalid memory action")

        target = item.get("target_memory_id")
        if action == "remember":
            if target is not None:
                raise RuntimeError(
                    "episode summary provider returned invalid memory target"
                )
        else:
            if isinstance(target, bool) or not isinstance(target, int):
                raise RuntimeError(
                    "episode summary provider returned invalid memory target"
                )
        evidence_message_id = item.get("evidence_message_id")
        evidence = item.get("evidence")
        if (
            isinstance(evidence_message_id, bool)
            or not isinstance(evidence_message_id, int)
            or not isinstance(evidence, str)
            or not evidence.strip()
            or len(evidence) > 500
        ):
            raise RuntimeError(
                "episode summary provider returned invalid memory evidence"
            )
        actions.append(dict(item))
    return actions


def parse_episode_summary_result(
    text: str, *, relevant_memory_ids: set[int] | None = None
) -> dict[str, object]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("episode summary provider returned invalid JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("claims"), list):
        raise RuntimeError("episode summary provider returned invalid claims")
    version = value.get("version")
    base_fields = {
        "version",
        "claims",
        "narrative_summary",
        "emotional_context",
        "outcomes",
    }
    if version == 2 and set(value) == base_fields:
        value["memory_actions"] = []
    elif version == 3 and set(value) == base_fields | {"memory_actions"}:
        value["memory_actions"] = _parse_memory_actions(
            value["memory_actions"], relevant_memory_ids
        )
    else:
        raise RuntimeError("episode summary provider returned invalid result")
    if not isinstance(value["outcomes"], list) or not all(
        isinstance(item, str) for item in value["outcomes"]
    ):
        raise RuntimeError("episode summary provider returned invalid outcomes")
    return value
