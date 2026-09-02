from collections.abc import Mapping, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import (
    DEFAULT_ACTION_LIMIT,
    DEFAULT_GAP_SECONDS,
    TranscriptGroup,
    text_value,
)


def _marker(
    moment_at: float, previous_at: float, gap: float, timezone: ZoneInfo
) -> str:
    """Render a time marker only where it changes how the text reads."""

    if moment_at <= 0:
        return ""
    moment = datetime.fromtimestamp(moment_at, timezone)
    if previous_at <= 0:
        return moment.isoformat(timespec="minutes")
    earlier = datetime.fromtimestamp(previous_at, timezone)
    if moment.date() != earlier.date():
        return moment.isoformat(timespec="minutes")
    if moment_at - previous_at >= gap:
        return moment.strftime("%H:%M")
    return ""

def _elapsed(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{max(1, round(seconds / 60))}m"

def turn_labels(groups: Sequence[TranscriptGroup]) -> dict[str, str]:
    ordered = [
        turn_id
        for group in groups
        for turn_id in group.turn_ids
        if turn_id
    ]
    return {
        turn_id: f"T{index}"
        for index, turn_id in enumerate(dict.fromkeys(ordered), 1)
    }

def _silence(
    group: TranscriptGroup, previous: TranscriptGroup | None
) -> dict[str, object] | None:
    """Mark the side that stayed quiet between two same-role Turns.

    Groups are per Turn, so two adjacent groups sharing a role can only mean one
    side spoke twice while the other said nothing. Both directions matter: an
    unanswered message is what Momoi needs before speaking a third time, and a
    Turn it deliberately ended without replying is a choice it should recall
    rather than an accident to repeat.

    The placeholder occupies the quiet side's slot without claiming anything was
    said. Current authority belongs only to the final request section, and both
    providers need the roles to alternate anyway.
    """

    if previous is None or previous.role != group.role:
        return None
    if group.role == "assistant":
        waited = max(0.0, group.started_at - previous.ended_at)
        return _message("user", f"[owner did not reply · {_elapsed(waited)} later]")
    return _message("assistant", "[ended the Turn without replying]")

def _message(role: str, text: str) -> dict[str, object]:
    """Build a message in the block form both provider adapters already take.

    Historical owner messages will carry images and other media alongside their
    text, and the current request already uses blocks, so the transcript uses
    one shape throughout rather than mixing bare strings with block lists.
    """

    return {"role": role, "content": [{"type": "text", "text": text}]}

def _action_line(records: Sequence[Mapping[str, object]]) -> str:
    """Render one run of calls to the same tool as a single trace line."""

    first = records[0]
    name = text_value(first.get("name"))
    subject = text_value(first.get("subject"))
    head = f"{name}({subject})" if subject else f"{name}()"
    if len(records) > 1:
        head += f" ×{len(records)}"
    failed = [record for record in records if not record.get("ok")]
    if failed:
        error = text_value(failed[0].get("error"))
        outcome = f"failed: {error}" if error else "failed"
        if len(failed) < len(records):
            outcome = f"{len(failed)} of {len(records)} {outcome}"
    else:
        outcome = "ok"
    refs = [
        text_value(record.get("ref"))
        for record in records
        if text_value(record.get("ref"))
    ]
    if refs:
        outcome += f" · ref={refs[0]}"
    return f"[tool_call] {head} -> {outcome}"

def _assistant_body(
    group: TranscriptGroup,
    records: Sequence[Mapping[str, object]],
    action_limit: int,
) -> list[str]:
    """Interleave what a Turn said with what it did, in the order it happened.

    Momoi narrates work as it goes, so the bubbles only make sense next to the
    calls they refer to. Consecutive calls to the same tool collapse into one
    line and a long run is truncated, because a Turn can issue ninety calls and
    the point is the shape of the work, not a full replay of it.
    """

    events: list[tuple[float, int, object]] = []
    for index, part in enumerate(group.parts):
        at = group.part_times[index] if index < len(group.part_times) else 0.0
        events.append((at, 1, part))
    for record in records:
        events.append((float(record.get("at") or 0.0), 0, record))
    events.sort(key=lambda item: (item[0], item[1]))

    lines: list[str] = []
    run: list[Mapping[str, object]] = []
    shown = 0
    dropped = 0

    def flush_run() -> None:
        nonlocal run, shown, dropped
        if not run:
            return
        if shown < action_limit:
            lines.append(_action_line(run))
            shown += 1
        else:
            dropped += len(run)
        run = []

    for _at, _kind, item in events:
        if isinstance(item, str):
            flush_run()
            lines.append(item)
            continue
        if run and text_value(run[0].get("name")) != text_value(item.get("name")):
            flush_run()
        run.append(item)
    flush_run()
    if dropped:
        lines.append(f"[tool_call] … {dropped} further calls]")
    return lines

def render_messages(
    groups: Sequence[TranscriptGroup],
    *,
    timezone: ZoneInfo,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    tool_activity: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    action_limit: int = DEFAULT_ACTION_LIMIT,
    labels: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Render groups as provider-neutral ``role`` / ``content`` messages."""

    messages: list[dict[str, object]] = []
    previous: TranscriptGroup | None = None
    for group in groups:
        silence = _silence(group, previous)
        if silence is not None:
            messages.append(silence)
        annotations = []
        group_labels = [
            str((labels or {}).get(turn_id) or "")
            for turn_id in group.turn_ids
            if (labels or {}).get(turn_id)
        ]
        if group_labels:
            annotations.append("turn=" + ",".join(dict.fromkeys(group_labels)))
        marker = _marker(
            group.started_at,
            previous.ended_at if previous else 0.0,
            gap_seconds,
            timezone,
        )
        if marker:
            annotations.append(marker)
        if group.uncertain:
            annotations.append("delivery uncertain")
        lines: list[str] = []
        if annotations:
            lines.append(f"[{' · '.join(annotations)}]")
        records = (
            [
                record
                for turn_id in group.turn_ids
                for record in (tool_activity or {}).get(turn_id, ())
            ]
            if group.role == "assistant"
            else []
        )
        if records:
            lines.extend(_assistant_body(group, records, action_limit))
        else:
            lines.extend(group.parts)
        messages.append(_message(group.role, "\n".join(lines)))
        previous = group
    return messages

def render_delivered_bubble_evidence(
    groups: Sequence[TranscriptGroup],
    *,
    timezone: ZoneInfo,
    tool_activity: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> str:
    """Render leading Momoi speech as evidence without fabricating dialogue."""

    rendered = render_messages(
        groups,
        timezone=timezone,
        tool_activity=tool_activity,
    )
    parts = [
        "Momoi bubbles already delivered before the retained owner transcript:"
    ]
    for message in rendered:
        content = "\n".join(
            str(block.get("text") or "")
            for block in message.get("content", [])
            if isinstance(block, Mapping)
        ).strip()
        if not content:
            continue
        label = (
            "Momoi"
            if message.get("role") == "assistant"
            else "Conversation state"
        )
        parts.append(f"[{label}]\n{content}")
    return "\n\n".join(parts) if len(parts) > 1 else ""
