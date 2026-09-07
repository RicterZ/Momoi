from collections.abc import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..agent.budget import TEXT_SIZER
from .models import (
    DEFAULT_ACTION_LIMIT,
    DEFAULT_GAP_SECONDS,
    VISIBLE_ASSISTANT_STATES,
    Transcript,
    TranscriptGroup,
    text_value,
)
from .rendering import render_bubble, render_event, render_review, render_messages


def _visible(row: Mapping[str, object]) -> bool:
    role = text_value(row.get("role"))
    if role in {"user", "event", "goal", "heartbeat"}:
        return bool(text_value(row.get("content")))
    if role != "assistant":
        return False
    state = text_value(row.get("delivery_state")) or "delivered"
    return state in VISIBLE_ASSISTANT_STATES and bool(text_value(row.get("content")))

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

    Dialogue is grouped by role and Turn; events remain separate historical
    inputs. Their reception time places them among the archived bubbles.
    """

    visible = sorted((row for row in rows if _visible(row)), key=_row_order)
    # Dialogue keeps its established message order. Event reception can precede
    # the archival of an older reply, so merge events by time, not archive ID.
    events = sorted(
        (row for row in visible if row.get("role") in {"event", "goal", "heartbeat"}),
        key=lambda row: (_row_order(row)[1], _row_order(row)[0]),
    )
    ordered = []
    event_index = 0
    for row in visible:
        if row.get("role") in {"event", "goal", "heartbeat"}:
            continue
        while event_index < len(events) and (
            _row_order(events[event_index])[1], _row_order(events[event_index])[0]
        ) <= (_row_order(row)[1], _row_order(row)[0]):
            ordered.append(events[event_index])
            event_index += 1
        ordered.append(row)
    ordered.extend(events[event_index:])
    groups: list[TranscriptGroup] = []
    parts: list[str] = []
    part_times: list[float] = []
    part_states: list[str] = []
    event_sources: list[str] = []
    message_ids: list[int] = []
    turn_ids: list[str] = []
    role = ""
    turn = ""
    started = 0.0
    ended = 0.0
    uncertain = False

    def flush() -> None:
        nonlocal parts, part_times, part_states, message_ids, turn_ids, role, turn, uncertain
        nonlocal event_sources
        if not parts:
            return
        text = "\n".join(
            render_event(
                part, message_ids[index], event_sources[index], part_times[index],
                ZoneInfo("UTC"),
            ) if role == "event" else (
                render_review(role, part, message_ids[index], part_times[index], ZoneInfo("UTC"))
                if role in {"goal", "heartbeat"} else render_bubble(part, delivery_state=part_states[index])
            )
            for index, part in enumerate(parts)
        )
        groups.append(
            TranscriptGroup(
                role=role,
                parts=tuple(parts),
                part_times=tuple(part_times),
                part_states=tuple(part_states),
                event_sources=tuple(event_sources),
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
        part_states = []
        event_sources = []
        message_ids = []
        turn_ids = []
        role = ""
        turn = ""
        uncertain = False

    for row in ordered:
        row_role = text_value(row.get("role"))
        row_turn = text_value(row.get("turn_id"))
        identifier, created = _row_order(row)
        if row_role != role or row_turn != turn:
            flush()
            role = row_role
            turn = row_turn
            started = created
        parts.append(text_value(row.get("content")))
        part_times.append(created)
        part_states.append(text_value(row.get("delivery_state")) or "delivered")
        event_sources.append(text_value(row.get("event_source")) or "webhook:unknown")
        message_ids.append(identifier)
        turn_ids.append(text_value(row.get("turn_id")))
        ended = created
        if text_value(row.get("delivery_state")) == "uncertain":
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
    """Split off committed Momoi speech that no user-role input precedes.

    Proactive Heartbeat, Goal and Webhook messages can open a window, and the
    Anthropic Messages API requires the first message to have the user role.
    Historical events can also occupy a user-role input without being owner
    speech. Leading assistant bubbles are returned separately with their delivery
    states for the caller to render as output evidence.
    """

    leading = 0
    while leading < len(groups) and groups[leading].role == "assistant":
        leading += 1
    return list(groups[:leading]), list(groups[leading:])

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
