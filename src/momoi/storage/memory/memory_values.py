from __future__ import annotations

from collections.abc import Mapping
from xml.sax.saxutils import escape, quoteattr

from ...memory.models import MemoryRecallQuery
from ...memory.records import MEMORY_ACTIVATIONS, memory_snapshot_fingerprint
from ...memory.text import estimate_tokens, truncate_tokens, token_chunk



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



def valid_scoped_memory_key(key: str) -> bool:
    """A scoped memory belongs to one stable workflow identity."""
    import re
    return bool(re.fullmatch(
        r"(?:goal\.[0-9a-f]{32}|heartbeat|webhook)\.[a-z0-9][a-z0-9_.-]*",
        key,
    ))

ALWAYS_MEMORY_KINDS = {"profile", "preference", "relationship"}


REFLECTION_MEMORY_CAUTION = (
    "Daily reflection memories are fallible and may be outdated or no longer "
    "applicable; use them only as supporting context and prefer current evidence."
)

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
