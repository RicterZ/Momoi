"""Compose the timeline, delegating journal replay and evidence projection.

Native tool pairs remain intact; old or partial turns use the explicit evidence
reader so replay never moves actions across an intervening input.
"""
from collections.abc import Mapping, Sequence
from datetime import datetime
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

from .native import render_exchanges
from .evidence import render_group_evidence
from .records import render_event, render_review, text_message

from .models import (
    DEFAULT_ACTION_LIMIT,
    TranscriptGroup,
    text_value,
)

def _elapsed(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{max(1, round(seconds / 60))}m"


OWNER_IDLE_GAP_SECONDS = 30 * 60


def owner_idle_gap_message(
    rows: Sequence[Mapping[str, object]], *, now: float, timezone: ZoneInfo,
) -> dict[str, object] | None:
    """Build transient context for an autonomous Turn after owner silence.

    Runtime records also use a user-role API message, so only persisted rows
    whose native role is exactly ``user`` establish the silence baseline.
    The returned message is request-local and must never be committed.
    """
    owner_rows = [row for row in rows if text_value(row.get("role")) == "user"]
    if not owner_rows:
        return None
    latest = max(owner_rows, key=lambda row: float(row.get("created_at") or 0.0))
    at = float(latest.get("created_at") or 0.0)
    if at <= 0:
        return None
    elapsed = max(0.0, now - at)
    if elapsed < OWNER_IDLE_GAP_SECONDS:
        return None
    local = datetime.fromtimestamp(at, timezone).isoformat(timespec="seconds")
    return text_message(
        "user",
        "[runtime time gap]\n"
        f"The owner's last message was at {local}; no newer owner message has arrived "
        f"for {_elapsed(elapsed)}. Re-evaluate time-sensitive activities using ordinary "
        "real-world durations; do not mechanically keep an earlier phase active.",
    )

def turn_labels(groups: Sequence[TranscriptGroup]) -> dict[str, str]:
    ordered = [
        turn_id
        for group in groups
        for turn_id in group.turn_ids
        if turn_id
    ]
    return {
        turn_id: f"T-{index}"
        for index, turn_id in enumerate(dict.fromkeys(ordered), 1)
    }

def render_pending_turns(labels: Sequence[str]) -> str:
    references = "\n".join(f"<turn id={quoteattr(label)} />" for label in labels)
    return "<pending_turns>\n" + (references or "none") + "\n</pending_turns>"

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
        if "queued" in previous.part_states:
            return text_message("user", "[previous assistant messages still being delivered]")
        waited = max(0.0, group.started_at - previous.ended_at)
        return text_message("user", f"[owner did not reply · {_elapsed(waited)} later]")
    return text_message("assistant", "[ended the Turn without replying]")

def render_messages(
    groups: Sequence[TranscriptGroup],
    *,
    timezone: ZoneInfo,
    tool_activity: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    action_limit: int = DEFAULT_ACTION_LIMIT,
    labels: Mapping[str, str] | None = None,
    native_exchanges: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    history_format: int = 4,
) -> list[dict[str, object]]:
    """Render groups as provider-neutral ``role`` / ``content`` messages."""

    messages: list[dict[str, object]] = []
    previous: TranscriptGroup | None = None
    replayed_turns: set[str] = set()
    speech_turns = {
        turn_id for group in groups if group.role == "assistant"
        for turn_id in group.turn_ids
    }
    # A webhook/owner update can split one Turn's visible messages around a
    # different input. Replaying its whole session at the first fragment would
    # move later actions before that input. Use the ordered legacy projection
    # for these split Turns until exchanges carry input boundaries.
    split_turns = {
        turn_id for turn_id in speech_turns
        if sum(turn_id in group.turn_ids and group.role == "assistant" for group in groups) > 1
    }
    if native_exchanges and split_turns:
        native_exchanges = {
            turn_id: exchanges for turn_id, exchanges in native_exchanges.items()
            if turn_id not in split_turns
        }
    def append(message: dict[str, object]) -> None:
        message["_history_turn_ids"] = list(group.turn_ids)
        messages.append(message)

    def extend(items: list[dict[str, object]]) -> None:
        for item in items:
            append(item)

    for group_index, group in enumerate(groups):
        if group.role in {"event", "goal", "heartbeat", "plan_step"}:
            lines = []
            for turn_id in group.turn_ids:
                if (labels or {}).get(turn_id):
                    lines.append(f"<turn id={quoteattr(labels[turn_id])} />")
            for index, content in enumerate(group.parts):
                lines.append(
                    render_event(
                        content, group.message_ids[index], group.event_sources[index],
                        group.part_times[index], timezone,
                    ) if group.role == "event" else render_review(
                        group.role, content, group.message_ids[index], group.part_times[index], timezone,
                    )
                )
            if group.role == "event" or history_format < 2:
                append(text_message("user", "\n".join(lines)))
            for turn_id in group.turn_ids:
                if (native_exchanges and native_exchanges.get(turn_id)
                        and turn_id not in speech_turns
                        and turn_id not in replayed_turns):
                    extend(render_exchanges(native_exchanges.get(turn_id, ()), history_format=history_format, timezone=timezone, has_speech=turn_id in speech_turns, mark_silence=group.role == "user"))
                    replayed_turns.add(turn_id)
            if group.role != "event" and history_format >= 2:
                append(text_message("user", "\n".join(lines)))
            # Runtime records are neither owner speech nor unanswered bubbles.
            previous = None
            continue
        silence = None if (
            previous is not None and previous.role == "user"
            and any(turn_id in replayed_turns for turn_id in previous.turn_ids)
        ) else _silence(group, previous)
        if silence is not None:
            append(silence)
        if group.role == "assistant" and native_exchanges:
            pending = [
                turn_id for turn_id in group.turn_ids
                if turn_id not in replayed_turns and native_exchanges.get(turn_id)
            ]
            if pending or any(turn_id in replayed_turns for turn_id in group.turn_ids):
                for turn_id in pending:
                    extend(render_exchanges(native_exchanges[turn_id], history_format=history_format, timezone=timezone, has_speech=turn_id in speech_turns, mark_silence=group.role == "user"))
                    replayed_turns.add(turn_id)
                # Keep the cached old window until its next compaction boundary.
                if history_format < 2 and group.parts:
                    states = [group.part_states[i] if i < len(group.part_states) else "delivered"
                              for i in range(len(group.parts))]
                    append(text_message("user", "[message delivery confirmation] " + ", ".join(states)))
                # Speech is already in the native reply projection or receipt.
                previous = group
                continue
        append(render_group_evidence(
            groups, group_index, timezone=timezone, tool_activity=tool_activity,
            action_limit=action_limit, labels=labels,
        ))
        if group.role == "user" and native_exchanges:
            for turn_id in group.turn_ids:
                if (native_exchanges.get(turn_id) and turn_id not in speech_turns
                        and turn_id not in replayed_turns):
                    extend(render_exchanges(native_exchanges.get(turn_id, ()), history_format=history_format, timezone=timezone, has_speech=turn_id in speech_turns, mark_silence=group.role == "user"))
                    replayed_turns.add(turn_id)
        previous = group
    return messages
