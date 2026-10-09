"""Stable ordering for scored retrieval results."""
import math
import time


def recall_item_score(item: dict[str, object], *, now: float | None = None) -> float:
    relevance = float(item.get("search_score") or 0.0)
    if relevance > 0:
        return relevance
    now = time.time() if now is None else now
    last_activity = float(item.get("last_activity_at") or 0.0)
    age = max(0.0, now - last_activity) if last_activity else 365 * 86400
    recency = 0.08 * math.exp(-age / (30 * 86400))
    return recency


def rank_recall_items(
    items: list[dict[str, object]],
    *,
    now: float | None = None,
) -> list[dict[str, object]]:
    stamp = time.time() if now is None else now
    return sorted(
        items,
        key=lambda item: (
            recall_item_score(item, now=stamp),
            float(item.get("last_activity_at") or 0.0),
            str(item.get("turn_id") or item.get("episode_id") or item.get("id") or ""),
        ),
        reverse=True,
    )
