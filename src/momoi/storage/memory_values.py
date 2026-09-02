from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from ..policies import MemoryPolicy


MEMORY_KINDS = {
    "profile",
    "preference",
    "relationship",
    "shared",
    "episodic",
    "routine",
}

MEMORY_ACTIVATIONS = {"always", "recent", "recall"}

ALWAYS_MEMORY_KINDS = {"profile", "preference", "relationship"}

RECENT_MEMORY_WINDOW_SECONDS = 30 * 24 * 60 * 60

_DEFAULT_MEMORY_POLICY = MemoryPolicy()

REFLECTION_MEMORY_CAUTION = (
    "Daily reflection memories are fallible and may be outdated or no longer "
    "applicable; use them only as supporting context and prefer current evidence."
)

@dataclass(frozen=True)
class MemoryRecallQuery:
    expression: str
    unit_ids: tuple[str, ...] = ()
    priority: int = 0
    semantic_expression: str = ""

    @property
    def dense_expression(self) -> str:
        return self.semantic_expression.strip() or self.expression.strip()

def memory_snapshot_fingerprint(memory: Mapping[str, object]) -> str:
    payload = {
        key: memory.get(key)
        for key in (
            "id",
            "kind",
            "key",
            "content",
            "activation",
            "expires_at",
            "source_event_id",
            "evidence_quote",
            "updated_at",
            "superseded_by",
        )
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

def format_reflection_memory(row: Mapping[str, object]) -> str:
    local_date = str(row.get("local_date") or "unknown")
    return (
        f"- [date={local_date} {row['kind']}:{row['key']}] "
        f"{row['content']}"
    )

def memory_expires_at(
    activation: str,
    ttl_hours: float,
    now: float,
    policy: MemoryPolicy = _DEFAULT_MEMORY_POLICY,
) -> float | None:
    if activation != "recent":
        return None
    hours = min(
        policy.recent_max_ttl_hours,
        max(policy.recent_min_ttl_hours, float(ttl_hours)),
    )
    return now + hours * 3600

def estimate_tokens(text: str) -> int:
    from ..runtime.agent.budget import TEXT_SIZER

    return TEXT_SIZER.estimate(text)

def truncate_tokens(text: str, token_budget: int) -> str:
    from ..runtime.agent.budget import MEMORY_TEXT_FITTER

    return MEMORY_TEXT_FITTER.truncate(text, token_budget)

def token_chunk(text: str, offset: int, token_budget: int) -> tuple[str, int | None]:
    if token_budget <= 0:
        raise ValueError("token budget must be positive")
    if offset < 0 or offset > len(text):
        raise ValueError("content offset is outside the message")
    remaining = text[offset:]
    if estimate_tokens(remaining) <= token_budget:
        return remaining, None
    marker = "…[continued]"
    if estimate_tokens(marker) >= token_budget:
        marker = ""
    low, high = 0, len(remaining)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(remaining[:middle] + marker) <= token_budget:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        low = 1
    return remaining[:low] + marker, offset + low

