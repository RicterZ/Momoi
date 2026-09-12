from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr



# The one classification used by both memory planes: confirmed memory (owner
# evidence) and reflection memory (daily review). They differ in provenance and
# reliability, not in what a fact is about, so they share this vocabulary.
#
# What memory holds, and what it deliberately does not:
#   - episodes narrate one-off events, so shared experiences are not a kind here;
#     they belong to episode summaries.
#   - current state owns temporary state, so memories never expire.
# A kind describes the subject of the fact, never its confidence.
MEMORY_KINDS = {
    "profile",           # who the owner is: identity, background, rhythm, habits
    "preference",        # what the owner wants: preferences, constraints, standing wording
    "relationship",      # the bond with the owner: nature, boundaries, forms of address, agreements
    "third_party",       # stable facts about other people in the world
    "practice",          # how to do things: reusable methods, decision procedures, tool usage
    "world_knowledge",   # facts about the world outside the relationship
    "self_insight",      # the agent's own tendencies, feelings, and patterns
    "cross_event_state", # a durable state that outlives the event that produced it
}

MEMORY_ACTIVATIONS = {"always", "recall"}

ALWAYS_MEMORY_KINDS = {"profile", "preference", "relationship"}


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
    # Empty means all canonical memory kinds.  This is deliberately part of
    # the query (rather than a post-filter) so sparse and dense ranking share
    # the same eligibility boundary.
    kinds: tuple[str, ...] = ()

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

def format_memory(row: Mapping[str, object]) -> str:
    attributes = {"id": row["id"], "kind": row["kind"], "key": row["key"]}
    if row.get("activation"):
        attributes["activation"] = row["activation"]
    header = " ".join(
        f"{key}={quoteattr(str(value))}" for key, value in attributes.items()
    )
    return f"<memory {header}>{escape(str(row['content']))}</memory>"


def format_reflection_memory(row: Mapping[str, object]) -> str:
    attributes = {
        "date": row.get("local_date"),
        "confidence": row.get("confidence"),
    }
    header = " ".join(
        f"{key}={quoteattr(str(value))}"
        for key, value in attributes.items()
        if value is not None
    )
    lines = [f"<reflection {header}>", f"  <content>{escape(str(row['content']))}</content>"]
    if row.get("evidence"):
        lines.append(f"  <evidence>{escape(str(row['evidence']))}</evidence>")
    lines.append("</reflection>")
    return "\n".join(lines)

def estimate_tokens(text: str) -> int:
    from ...runtime.agent.budget import TEXT_SIZER

    return TEXT_SIZER.estimate(text)

def truncate_tokens(text: str, token_budget: int) -> str:
    from ...runtime.agent.budget import MEMORY_TEXT_FITTER

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
