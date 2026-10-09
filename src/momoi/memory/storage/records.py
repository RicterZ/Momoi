"""Persisted memory records and optimistic concurrency fingerprints."""
import hashlib
import json
from collections.abc import Mapping
from typing import TypedDict

from ..metadata import MemoryMeta

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

MEMORY_ACTIVATIONS = {"always", "recall", "scoped"}


class ActiveMemory(TypedDict):
    id: int
    kind: str
    key: str
    content: str
    importance: float
    meta: MemoryMeta


class InventoryMemory(ActiveMemory):
    activation: str
    authority: str
    source_event_id: str | None
    evidence_quote: str | None
    created_at: float
    updated_at: float
    expires_at: float | None
    superseded_by: int | None


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
    payload["meta"] = memory.get("meta", {"tags": []})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
