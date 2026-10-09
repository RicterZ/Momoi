"""Persisted memory records and optimistic concurrency fingerprints."""
import hashlib
import json
from collections.abc import Mapping
from typing import TypedDict

MEMORY_ACTIVATIONS = {"always", "recall", "scoped"}


class ActiveMemory(TypedDict):
    id: int
    kind: str
    key: str
    content: str
    importance: float


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
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
