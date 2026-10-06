"""Project only actual dialogue, keeping internal planner records out of replies."""
from datetime import datetime
from zoneinfo import ZoneInfo


def visible_dialogue(rows, *, timezone="UTC"):
    selected = []
    for row in rows:
        role = row.get("role")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant" and row.get("delivery_state") != "delivered":
            continue
        text = str(row.get("content") or "").strip()
        if not text:
            continue
        if role == "user":
            timestamp = datetime.fromtimestamp(float(row["created_at"]), ZoneInfo(str(timezone))).isoformat()
            text = f'<message time="{timestamp}">\n{text}\n</message>'
        selected.append({"role": role, "content": text})
    return selected
