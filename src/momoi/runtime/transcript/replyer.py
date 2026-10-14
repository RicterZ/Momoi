"""Project only actual dialogue, keeping internal planner records out of replies."""
from datetime import datetime
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape
from ...storage.delivery.actions import QQ_POKE_MARKER, QQ_POKE_DESCRIPTION


def visible_dialogue(rows, *, timezone="UTC", memory_events=()):
    selected = []
    turn_ids = []
    for row in rows:
        role = row.get("role")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant" and row.get("delivery_state") != "delivered":
            continue
        text = str(row.get("content") or "").strip()
        if not text:
            continue
        if text == QQ_POKE_MARKER:
            text = QQ_POKE_DESCRIPTION
        if role == "user":
            timestamp = datetime.fromtimestamp(float(row["created_at"]), ZoneInfo(str(timezone))).isoformat()
            text = f'[message][{timestamp}][user] {escape(text)}'
        selected.append({"role": role, "content": text})
        turn_ids.append(str(row.get("turn_id") or ""))
    # Match Planner: changes follow the last visible message of their anchor turn.
    for event in reversed(memory_events):
        indexes = [i for i, turn_id in enumerate(turn_ids) if turn_id == event["anchor"]]
        index = max(indexes) + 1 if indexes else 0
        selected.insert(index, {"role": "user", "content": event["content"],
                                "_memory_change": event["revision"], "_context_prefix": True})
        turn_ids.insert(index, None)
    return selected
