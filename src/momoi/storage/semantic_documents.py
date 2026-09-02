from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .integrity import decode_stored_json
from .memory_values import estimate_tokens, token_chunk


QUERY_TEMPLATE_VERSION = 1

DOCUMENT_TEMPLATE_VERSION = 1

SEMANTIC_PROVIDER = "fastembed"

EPISODE_CHUNK_TOKENS = 420

EPISODE_CHUNK_OVERLAP_TOKENS = 40

@dataclass(frozen=True)
class SemanticDocument:
    document_type: str
    source_id: str
    parent_id: str
    chunk_index: int
    content: str
    source_ids: tuple[object, ...] = ()
    starts_at: float | None = None
    ends_at: float | None = None

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

def encode_vector(vector: Iterable[float], dimensions: int) -> bytes:
    array = np.asarray(tuple(vector), dtype="<f4")
    if array.ndim != 1 or array.size != dimensions:
        raise ValueError("embedding dimension mismatch")
    if not np.isfinite(array).all():
        raise ValueError("embedding contains non-finite values")
    norm = float(np.linalg.norm(array))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding has zero or invalid norm")
    return (array / norm).astype("<f4", copy=False).tobytes()

def decode_vector(blob: object, dimensions: int) -> np.ndarray:
    if not isinstance(blob, bytes) or len(blob) != dimensions * 4:
        raise ValueError("invalid embedding byte length")
    vector = np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)
    if not np.isfinite(vector).all():
        raise ValueError("embedding contains non-finite values")
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding has zero or invalid norm")
    if abs(norm - 1.0) > 1e-3:
        vector /= norm
    return vector

def _json_strings(value: object) -> list[str]:
    parsed = decode_stored_json(
        value or "[]",
        entity="semantic_document",
        record_id="unknown",
        field="terms_json",
        expected_type=list,
        fallback=[],
    )
    return [str(item).strip() for item in parsed if str(item).strip()]

def _episode_summary_document(row: sqlite3.Row) -> SemanticDocument | None:
    parts: list[str] = []
    for label, value in (
        ("Title", row["title"]),
        ("Topics", "；".join(_json_strings(row["topics_json"]))),
        ("Entities", "；".join(_json_strings(row["entities_json"]))),
        ("Narrative", row["narrative_summary"]),
        ("Outcomes", "；".join(_json_strings(row["outcomes_json"]))),
        ("Evidence summary", row["working_summary"]),
        ("Summary", row["summary"]),
    ):
        text = str(value or "").strip()
        if text:
            parts.append(f"{label}: {text}")
    if not parts:
        return None
    episode_id = str(row["id"])
    return SemanticDocument(
        "episode_summary", episode_id, episode_id, 0, "\n".join(parts)
    )

def _message_parts(row: sqlite3.Row) -> list[str]:
    role = {
        "user": "OWNER",
        "event": "EVENT",
    }.get(str(row["role"]), "MOMOI")
    label = (
        f"[{role} turn={row['turn_id']} ordinal={row['ordinal']} "
        f"delivery={row['delivery_state']}] "
    )
    content = str(row["content"])
    budget = max(1, EPISODE_CHUNK_TOKENS - estimate_tokens(label))
    parts: list[str] = []
    offset = 0
    while offset < len(content):
        piece, next_offset = token_chunk(content, offset, budget)
        parts.append(label + piece)
        if next_offset is None:
            break
        offset = next_offset
    return parts or [label]

def _episode_turn_documents(
    episode_id: str, rows: list[sqlite3.Row]
) -> list[SemanticDocument]:
    by_turn: dict[str, list[sqlite3.Row]] = {}
    order: list[str] = []
    for row in rows:
        turn_id = str(row["turn_id"])
        if turn_id not in by_turn:
            by_turn[turn_id] = []
            order.append(turn_id)
        by_turn[turn_id].append(row)
    documents: list[SemanticDocument] = []
    for turn_id in order:
        turn_rows = by_turn[turn_id]
        parts = [part for row in turn_rows for part in _message_parts(row)]
        chunks: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for part in parts:
            size = estimate_tokens(part)
            if current and current_tokens + size > EPISODE_CHUNK_TOKENS:
                chunks.append(current)
                overlap: list[str] = []
                overlap_tokens = 0
                for prior in reversed(current):
                    prior_size = estimate_tokens(prior)
                    if (
                        overlap
                        and overlap_tokens + prior_size > EPISODE_CHUNK_OVERLAP_TOKENS
                    ):
                        break
                    overlap.insert(0, prior)
                    overlap_tokens += prior_size
                current = overlap
                current_tokens = overlap_tokens
            current.append(part)
            current_tokens += size
        if current:
            chunks.append(current)
        message_ids = tuple(int(row["id"]) for row in turn_rows)
        starts_at = min(float(row["created_at"]) for row in turn_rows)
        ends_at = max(float(row["created_at"]) for row in turn_rows)
        for index, chunk in enumerate(chunks):
            documents.append(
                SemanticDocument(
                    "episode_turn",
                    json.dumps([episode_id, turn_id], separators=(",", ":")),
                    episode_id,
                    index,
                    "\n".join(chunk),
                    message_ids,
                    starts_at,
                    ends_at,
                )
            )
    return documents

