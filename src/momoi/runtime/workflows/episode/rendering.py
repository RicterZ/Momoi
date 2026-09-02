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
    episode: Mapping[str, object], messages: Sequence[Mapping[str, object]]
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

    return (
        "<episode>\n"
        f"id: {_text(episode.get('id'))}\n"
        f"title: {_text(episode.get('title'))}\n"
        "</episode>\n\n<previous_verified_claims>\n"
        + ("\n\n".join(claim_blocks) or "none")
        + "\n</previous_verified_claims>\n\n<new_messages>\n"
        + ("\n\n".join(message_blocks) or "none")
        + "\n</new_messages>"
    )
