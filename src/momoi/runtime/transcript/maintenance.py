"""One standard transcript and one local ID mapping for maintenance requests."""

from xml.sax.saxutils import quoteattr

from .building import build_transcript
from .rendering import render_messages, turn_labels


def maintenance_transcript(store, rows, required_turn_ids):
    activity = store.turn_activity(list(dict.fromkeys(
        [str(row["turn_id"]) for row in rows] + list(required_turn_ids)
    )))
    transcript = build_transcript(rows, timezone=store.timezone, tool_activity=activity)
    groups = [*transcript.orphaned, *transcript.groups]
    labels = turn_labels(groups)
    messages = render_messages(
        groups, timezone=store.timezone, tool_activity=activity, labels=labels,
    )
    if messages and messages[0]["role"] == "assistant":
        messages.insert(0, {"role": "user", "content": "<conversation_history />"})
    # Silent background Turns can have no visible timeline record at all.
    # Give them an explicit empty reference, never fabricate speech or replay tools.
    for turn_id in required_turn_ids:
        if turn_id not in labels:
            labels[turn_id] = f"T-{len(labels) + 1}"
            messages.append({
                "role": "user",
                "content": f"<turn id={quoteattr(labels[turn_id])} evidence=\"none\" />",
            })
    return messages, labels
