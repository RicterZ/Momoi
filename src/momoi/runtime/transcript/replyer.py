"""Project only actual dialogue, keeping internal planner records out of replies."""
from datetime import datetime
from zoneinfo import ZoneInfo

from ...storage import estimate_tokens

REPLYER_HISTORY_MESSAGES = 48
REPLYER_HISTORY_TOKENS = 5000


def visible_dialogue(rows, *, timezone="UTC", token_budget=REPLYER_HISTORY_TOKENS):
    selected = []
    used = 0
    for row in reversed(rows):
        role = row.get("role")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant" and row.get("delivery_state") != "delivered":
            continue
        text = str(row.get("content") or "").strip()
        if not text:
            continue
        size = estimate_tokens(text)
        if used + size > token_budget:
            break
        used += size
        if role == "user":
            timestamp = datetime.fromtimestamp(float(row["created_at"]), ZoneInfo(str(timezone))).isoformat()
            text = f'<message role="user" time="{timestamp}">\n{text}\n</message>'
        selected.append({"role": role, "content": text})
        if len(selected) >= REPLYER_HISTORY_MESSAGES:
            break
    return list(reversed(selected))
