"""Project stored conversation rows into native protocol messages.

Momoi's shared conversation is persisted as individual rows: one row per owner
message and one row per delivered Momoi bubble. This module turns those rows
into the alternating ``user`` / ``assistant`` messages that a provider expects,
so that the model reads a conversation instead of a report about one.

The module is deliberately pure. It takes plain mappings, never touches storage
or providers, and makes no network calls, which keeps the projection rules
directly unit-testable and keeps chronology decisions in one place.

Chronology comes from the persisted row order rather than ``created_at``.
Owner rows are written when their Turn binds its input while bubble rows are
written as delivery happens, so the stored timestamps of one Turn can invert
even though the insertion order stays correct.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .agent.budget import TEXT_SIZER

VISIBLE_ASSISTANT_STATES = frozenset({"delivered", "uncertain"})
DEFAULT_GAP_SECONDS = 30 * 60
# A Turn can issue dozens of calls; the trace should show its shape, not replay it.
DEFAULT_ACTION_LIMIT = 12


@dataclass(frozen=True)
class TranscriptGroup:
    """One protocol message built from consecutive same-role rows."""

    role: str
    parts: tuple[str, ...]
    part_times: tuple[float, ...]
    message_ids: tuple[int, ...]
    turn_ids: tuple[str, ...]
    started_at: float
    ended_at: float
    uncertain: bool = False
    token_estimate: int = field(default=0, compare=False)


def _text(value: object) -> str:
    return str(value or "").strip()


def _visible(row: Mapping[str, object]) -> bool:
    role = _text(row.get("role"))
    if role == "user":
        return bool(_text(row.get("content")))
    if role != "assistant":
        return False
    state = _text(row.get("delivery_state")) or "delivered"
    return state in VISIBLE_ASSISTANT_STATES and bool(_text(row.get("content")))


def _row_order(row: Mapping[str, object]) -> tuple[int, float]:
    try:
        identifier = int(row.get("id") or 0)
    except (TypeError, ValueError):
        identifier = 0
    try:
        created = float(row.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created = 0.0
    return identifier, created


def build_groups(rows: Iterable[Mapping[str, object]]) -> list[TranscriptGroup]:
    """Group visible rows into one message per role per runtime Turn.

    A Turn is the unit of Momoi speaking. Within one Turn it may call
    ``send_bubbles`` several times around tool work, but the owner simply sees
    consecutive bubbles, and the tool work between them never enters history.
    Grouping by Turn therefore needs no timing heuristic, and it gives
    consecutive assistant groups one exact meaning: Momoi opened a new Turn and
    spoke again without the owner answering.
    """

    visible = sorted((row for row in rows if _visible(row)), key=_row_order)
    groups: list[TranscriptGroup] = []
    parts: list[str] = []
    part_times: list[float] = []
    message_ids: list[int] = []
    turn_ids: list[str] = []
    role = ""
    turn = ""
    started = 0.0
    ended = 0.0
    uncertain = False

    def flush() -> None:
        nonlocal parts, part_times, message_ids, turn_ids, role, turn, uncertain
        if not parts:
            return
        text = "\n".join(parts)
        groups.append(
            TranscriptGroup(
                role=role,
                parts=tuple(parts),
                part_times=tuple(part_times),
                message_ids=tuple(message_ids),
                turn_ids=tuple(dict.fromkeys(turn_ids)),
                started_at=started,
                ended_at=ended,
                uncertain=uncertain,
                token_estimate=TEXT_SIZER.estimate(text),
            )
        )
        parts = []
        part_times = []
        message_ids = []
        turn_ids = []
        role = ""
        turn = ""
        uncertain = False

    for row in visible:
        row_role = _text(row.get("role"))
        row_turn = _text(row.get("turn_id"))
        identifier, created = _row_order(row)
        if row_role != role or row_turn != turn:
            flush()
            role = row_role
            turn = row_turn
            started = created
        parts.append(_text(row.get("content")))
        part_times.append(created)
        message_ids.append(identifier)
        turn_ids.append(_text(row.get("turn_id")))
        ended = created
        if _text(row.get("delivery_state")) == "uncertain":
            uncertain = True
    flush()
    return groups


def select_groups(
    groups: Sequence[TranscriptGroup],
    *,
    max_groups: int = 0,
    token_budget: int = 0,
) -> list[TranscriptGroup]:
    """Keep the most recent whole groups that fit, never splitting one."""

    selected = list(groups)
    if max_groups > 0:
        selected = selected[-max_groups:]
    if token_budget > 0:
        kept: list[TranscriptGroup] = []
        used = 0
        for group in reversed(selected):
            if kept and used + group.token_estimate > token_budget:
                break
            used += group.token_estimate
            kept.append(group)
        selected = list(reversed(kept))
    return selected


def partition_for_protocol(
    groups: Sequence[TranscriptGroup],
) -> tuple[list[TranscriptGroup], list[TranscriptGroup]]:
    """Split off delivered Momoi speech that no owner message precedes.

    Proactive Heartbeat, Goal and Webhook messages can open a window, and the
    Anthropic Messages API requires the first message to be the user's. Those
    bubbles are still real speech the owner saw, so they are returned separately
    for the caller to render as delivered-output evidence rather than dropped or
    turned into a fabricated owner message.
    """

    leading = 0
    while leading < len(groups) and groups[leading].role != "user":
        leading += 1
    return list(groups[:leading]), list(groups[leading:])


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
    name = _text(first.get("name"))
    subject = _text(first.get("subject"))
    head = f"{name}({subject})" if subject else f"{name}()"
    if len(records) > 1:
        head += f" ×{len(records)}"
    failed = [record for record in records if not record.get("ok")]
    if failed:
        error = _text(failed[0].get("error"))
        outcome = f"failed: {error}" if error else "failed"
        if len(failed) < len(records):
            outcome = f"{len(failed)} of {len(records)} {outcome}"
    else:
        outcome = "ok"
    refs = [_text(record.get("ref")) for record in records if _text(record.get("ref"))]
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
        if run and _text(run[0].get("name")) != _text(item.get("name")):
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


@dataclass(frozen=True)
class Transcript:
    """A protocol-valid transcript plus the speech that could not enter it."""

    messages: list[dict[str, object]]
    groups: list[TranscriptGroup]
    orphaned: list[TranscriptGroup]

    @property
    def token_estimate(self) -> int:
        return sum(group.token_estimate for group in self.groups)


def build_transcript(
    rows: Iterable[Mapping[str, object]],
    *,
    timezone: ZoneInfo,
    max_groups: int = 0,
    token_budget: int = 0,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    tool_activity: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    action_limit: int = DEFAULT_ACTION_LIMIT,
) -> Transcript:
    """Project stored rows into native messages and the groups behind them."""

    selected = select_groups(
        build_groups(rows), max_groups=max_groups, token_budget=token_budget
    )
    orphaned, groups = partition_for_protocol(selected)
    return Transcript(
        messages=render_messages(
            groups,
            timezone=timezone,
            gap_seconds=gap_seconds,
            tool_activity=tool_activity,
            action_limit=action_limit,
        ),
        groups=groups,
        orphaned=orphaned,
    )
