from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .memory import estimate_tokens, token_chunk
from .integrity import decode_stored_json

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


class SemanticStore:
    _db: sqlite3.Connection

    def semantic_space(self, *, state: str = "active") -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM semantic_spaces WHERE state=? ORDER BY created_at DESC LIMIT 1",
            (state,),
        ).fetchone()
        return dict(row) if row else None

    def ensure_semantic_space(
        self,
        *,
        model: str,
        dimensions: int,
        calibration_profile: str,
        state: str = "building",
    ) -> dict[str, object]:
        row = self._db.execute(
            """SELECT * FROM semantic_spaces
               WHERE provider=? AND model=? AND dimensions=?
                 AND query_template_version=? AND document_template_version=?
                 AND calibration_profile=?""",
            (
                SEMANTIC_PROVIDER,
                model,
                dimensions,
                QUERY_TEMPLATE_VERSION,
                DOCUMENT_TEMPLATE_VERSION,
                calibration_profile,
            ),
        ).fetchone()
        if row is not None:
            if state == "building" and row["state"] == "retired":
                with self._db:
                    self._db.execute(
                        "UPDATE semantic_spaces SET state='building', activated_at=NULL WHERE id=?",
                        (row["id"],),
                    )
                row = self._db.execute(
                    "SELECT * FROM semantic_spaces WHERE id=?", (row["id"],)
                ).fetchone()
            return dict(row)
        now = time.time()
        space_id = f"sem-{uuid.uuid4().hex}"
        with self._db:
            self._db.execute(
                """INSERT INTO semantic_spaces
                   (id, provider, model, dimensions, query_template_version,
                    document_template_version, calibration_profile, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    space_id,
                    SEMANTIC_PROVIDER,
                    model,
                    dimensions,
                    QUERY_TEMPLATE_VERSION,
                    DOCUMENT_TEMPLATE_VERSION,
                    calibration_profile,
                    state,
                    now,
                ),
            )
        return dict(
            self._db.execute(
                "SELECT * FROM semantic_spaces WHERE id=?", (space_id,)
            ).fetchone()
        )

    def activate_semantic_space(self, space_id: str) -> None:
        status = self.semantic_status(space_id)
        if status["pending"] or status["encoding"] or status["retry"]:
            raise ValueError("semantic space still has unfinished documents")
        if status["eligible_source_coverage"] < 1.0:
            raise ValueError("semantic space source coverage is incomplete")
        now = time.time()
        with self._db:
            row = self._db.execute(
                "SELECT state FROM semantic_spaces WHERE id=?", (space_id,)
            ).fetchone()
            if row is None:
                raise ValueError("semantic space not found")
            self._db.execute(
                "UPDATE semantic_spaces SET state='retired' WHERE state='active' AND id<>?",
                (space_id,),
            )
            self._db.execute(
                "UPDATE semantic_spaces SET state='active', activated_at=? WHERE id=?",
                (now, space_id),
            )

    def recover_semantic_encoding(self) -> int:
        with self._db:
            cursor = self._db.execute(
                """UPDATE semantic_documents
                   SET state='pending', retry_at=NULL, last_error='worker_restarted'
                   WHERE state='encoding'"""
            )
        return cursor.rowcount

    def _eligible_source_ids(self) -> dict[str, set[str]]:
        now = time.time()
        confirmed = {
            str(row["id"])
            for row in self._db.execute(
                """SELECT id FROM memories AS m
                   WHERE superseded_by IS NULL AND activation='recall'
                     AND (expires_at IS NULL OR expires_at>?)
                     AND NOT EXISTS (
                         SELECT 1 FROM memory_tombstones AS t
                         WHERE t.kind=m.kind AND t.key=m.key
                     )""",
                (now,),
            )
        }
        reflections = {
            str(row["id"])
            for row in self._db.execute("SELECT id FROM reflection_memories")
        }
        episodes = {
            str(row["id"])
            for row in self._db.execute(
                """SELECT e.id FROM conversation_episodes AS e
                   WHERE e.status='closed' AND e.summary_claimed_at IS NULL
                     AND e.summarized_through_ordinal >= COALESCE((
                         SELECT MAX(et.ordinal) FROM episode_turns AS et
                         WHERE et.episode_id=e.id
                     ), 0)
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS et
                         JOIN messages AS m ON m.turn_id=et.turn_id
                         WHERE et.episode_id=e.id AND m.delivery_state='queued'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS et
                         JOIN self_state AS s ON s.pending_reply_turn_id=et.turn_id
                         WHERE et.episode_id=e.id AND s.id=1
                           AND s.pending_reply_expectation<>''
                     )
                     AND (e.title<>'' OR e.working_summary<>'' OR e.narrative_summary<>''
                          OR e.summary<>'' OR EXISTS (
                              SELECT 1 FROM episode_turns AS et
                              JOIN messages AS m ON m.turn_id=et.turn_id
                              WHERE et.episode_id=e.id
                                AND (m.role IN ('user','event') OR m.delivery_state IN
                                     ('delivered','uncertain','internal'))
                          ))"""
            )
        }
        return {
            "confirmed_memory": confirmed,
            "reflection_memory": reflections,
            "episode": episodes,
        }

    def _episode_is_eligible(self, episode_id: str) -> bool:
        return (
            self._db.execute(
                """SELECT 1 FROM conversation_episodes AS e
               WHERE e.id=? AND e.status='closed' AND e.summary_claimed_at IS NULL
                 AND e.summarized_through_ordinal >= COALESCE((
                     SELECT MAX(et.ordinal) FROM episode_turns AS et
                     WHERE et.episode_id=e.id
                 ), 0)
                 AND NOT EXISTS (
                     SELECT 1 FROM episode_turns AS et
                     JOIN messages AS m ON m.turn_id=et.turn_id
                     WHERE et.episode_id=e.id AND m.delivery_state='queued'
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM episode_turns AS et
                     JOIN self_state AS s ON s.pending_reply_turn_id=et.turn_id
                     WHERE et.episode_id=e.id AND s.id=1
                       AND s.pending_reply_expectation<>''
                 )
                 AND (e.title<>'' OR e.working_summary<>'' OR e.narrative_summary<>''
                      OR e.summary<>'' OR EXISTS (
                          SELECT 1 FROM episode_turns AS et
                          JOIN messages AS m ON m.turn_id=et.turn_id
                          WHERE et.episode_id=e.id
                            AND (m.role IN ('user','event') OR m.delivery_state IN
                                 ('delivered','uncertain','internal'))
                      ))""",
                (episode_id,),
            ).fetchone()
            is not None
        )

    def reconcile_semantic_sources(self, space_id: str) -> int:
        eligible = self._eligible_source_ids()
        queued = 0
        now = time.time()
        with self._db:
            for source_type, source_ids in eligible.items():
                for source_id in source_ids:
                    expected_documents, _exists = self._source_documents(
                        source_type, source_id
                    )
                    expected = {
                        (
                            document.document_type,
                            document.source_id,
                            document.chunk_index,
                        ): document.content_sha256
                        for document in expected_documents
                    }
                    if source_type == "episode":
                        rows = self._db.execute(
                            """SELECT document_type, source_id, chunk_index,
                                      content_sha256, state
                               FROM semantic_documents
                               WHERE space_id=? AND (parent_id=? OR
                                   document_type='episode_summary' AND source_id=?)""",
                            (space_id, source_id, source_id),
                        ).fetchall()
                    else:
                        rows = self._db.execute(
                            """SELECT document_type, source_id, chunk_index,
                                      content_sha256, state
                               FROM semantic_documents
                               WHERE space_id=? AND document_type=? AND source_id=?""",
                            (space_id, source_type, source_id),
                        ).fetchall()
                    actual = {
                        (
                            str(row["document_type"]),
                            str(row["source_id"]),
                            int(row["chunk_index"]),
                        ): str(row["content_sha256"])
                        for row in rows
                        if row["state"] != "inactive"
                    }
                    if actual == expected:
                        continue
                    self._db.execute(
                        """INSERT INTO semantic_dirty_sources
                           (source_type, source_id, changed_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(source_type, source_id) DO UPDATE SET
                             changed_at=excluded.changed_at, claimed_at=NULL,
                             retry_at=NULL, last_error=NULL""",
                        (source_type, source_id, now),
                    )
                    queued += 1
            rows = self._db.execute(
                """SELECT DISTINCT d.document_type, d.source_id, d.parent_id
                   FROM semantic_documents AS d
                   WHERE d.space_id=?""",
                (space_id,),
            ).fetchall()
            stale_sources: set[tuple[str, str]] = set()
            for row in rows:
                document_type = str(row["document_type"])
                source_type = (
                    "episode" if document_type.startswith("episode_") else document_type
                )
                source_id = (
                    str(row["parent_id"])
                    if document_type == "episode_turn"
                    else str(row["source_id"])
                )
                stale_sources.add((source_type, source_id))
            for source_type, source_id in stale_sources:
                if source_id in eligible[source_type]:
                    continue
                self._db.execute(
                    """INSERT INTO semantic_dirty_sources
                       (source_type, source_id, changed_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(source_type, source_id) DO UPDATE SET
                         changed_at=excluded.changed_at, claimed_at=NULL,
                         retry_at=NULL, last_error=NULL""",
                    (source_type, source_id, now),
                )
                queued += 1
        return queued

    def claim_semantic_sources(self, limit: int = 16) -> list[dict[str, object]]:
        now = time.time()
        with self._db:
            rows = self._db.execute(
                """SELECT source_type, source_id, changed_at
                   FROM semantic_dirty_sources
                   WHERE claimed_at IS NULL AND COALESCE(retry_at, 0)<=?
                   ORDER BY changed_at LIMIT ?""",
                (now, max(1, limit)),
            ).fetchall()
            claimed: list[dict[str, object]] = []
            for row in rows:
                cursor = self._db.execute(
                    """UPDATE semantic_dirty_sources
                       SET claimed_at=?, attempts=attempts+1
                       WHERE source_type=? AND source_id=? AND claimed_at IS NULL
                         AND changed_at=?""",
                    (now, row["source_type"], row["source_id"], row["changed_at"]),
                )
                if cursor.rowcount:
                    claimed.append(dict(row))
        return claimed

    def _source_documents(
        self, source_type: str, source_id: str
    ) -> tuple[list[SemanticDocument], bool]:
        if source_type == "confirmed_memory":
            row = self._db.execute(
                """SELECT id, kind, key, content FROM memories AS m
                   WHERE id=? AND superseded_by IS NULL AND activation='recall'
                     AND (expires_at IS NULL OR expires_at>?)
                     AND NOT EXISTS (
                         SELECT 1 FROM memory_tombstones AS t
                         WHERE t.kind=m.kind AND t.key=m.key
                     )""",
                (source_id, time.time()),
            ).fetchone()
            if row is None:
                return [], False
            return [
                SemanticDocument(
                    source_type,
                    source_id,
                    "",
                    0,
                    f"Kind: {row['kind']}\nKey: {row['key']}\nContent: {row['content']}",
                )
            ], True
        if source_type == "reflection_memory":
            row = self._db.execute(
                "SELECT id, kind, key, content FROM reflection_memories WHERE id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                return [], False
            return [
                SemanticDocument(
                    source_type,
                    source_id,
                    "",
                    0,
                    f"Kind: {row['kind']}\nKey: {row['key']}\nContent: {row['content']}",
                )
            ], True
        if source_type != "episode":
            raise ValueError("unknown semantic source type")
        eligible = self._episode_is_eligible(source_id)
        if not eligible:
            return [], self._db.execute(
                "SELECT 1 FROM conversation_episodes WHERE id=?", (source_id,)
            ).fetchone() is not None
        episode = self._db.execute(
            "SELECT * FROM conversation_episodes WHERE id=?", (source_id,)
        ).fetchone()
        if episode is None:
            return [], False
        rows = self._db.execute(
            """SELECT et.ordinal, et.relation, m.id, m.turn_id, m.role,
                      m.content, m.created_at, m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?
                 AND (m.role IN ('user','event') OR m.delivery_state IN
                      ('delivered','uncertain','internal'))
               ORDER BY et.ordinal, m.id""",
            (source_id,),
        ).fetchall()
        summary = _episode_summary_document(episode)
        return ([summary] if summary else []) + _episode_turn_documents(
            source_id, list(rows)
        ), True

    def materialize_semantic_source(self, claim: dict[str, object]) -> int:
        source_type = str(claim["source_type"])
        source_id = str(claim["source_id"])
        changed_at = float(claim["changed_at"])
        documents, source_exists = self._source_documents(source_type, source_id)
        expected = {
            (doc.document_type, doc.source_id, doc.chunk_index): doc
            for doc in documents
        }
        spaces = self._db.execute(
            "SELECT id, dimensions FROM semantic_spaces WHERE state IN ('building','active')"
        ).fetchall()
        now = time.time()
        changed = 0
        with self._db:
            for space in spaces:
                space_id = str(space["id"])
                if source_type == "episode":
                    existing = self._db.execute(
                        """SELECT * FROM semantic_documents
                           WHERE space_id=? AND (parent_id=? OR
                               document_type='episode_summary' AND source_id=?)""",
                        (space_id, source_id, source_id),
                    ).fetchall()
                else:
                    existing = self._db.execute(
                        """SELECT * FROM semantic_documents
                           WHERE space_id=? AND document_type=? AND source_id=?""",
                        (space_id, source_type, source_id),
                    ).fetchall()
                by_key = {
                    (
                        str(row["document_type"]),
                        str(row["source_id"]),
                        int(row["chunk_index"]),
                    ): row
                    for row in existing
                }
                for key, row in by_key.items():
                    if key in expected:
                        continue
                    if source_type == "episode" and source_exists:
                        if row["state"] != "inactive":
                            self._db.execute(
                                """UPDATE semantic_documents SET state='inactive',
                                   updated_at=? WHERE space_id=? AND document_type=?
                                   AND source_id=? AND chunk_index=?""",
                                (now, space_id, *key),
                            )
                            changed += 1
                    else:
                        self._db.execute(
                            """DELETE FROM semantic_documents WHERE space_id=?
                               AND document_type=? AND source_id=? AND chunk_index=?""",
                            (space_id, *key),
                        )
                        changed += 1
                for key, document in expected.items():
                    existing_row = by_key.get(key)
                    content_hash = document.content_sha256
                    source_ids_json = json.dumps(
                        list(document.source_ids), separators=(",", ":")
                    )
                    if existing_row is None:
                        self._db.execute(
                            """INSERT INTO semantic_documents
                               (space_id, document_type, source_id, parent_id,
                                chunk_index, content, content_sha256,
                                source_ids_json, starts_at, ends_at, state,
                                created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                            (
                                space_id,
                                document.document_type,
                                document.source_id,
                                document.parent_id,
                                document.chunk_index,
                                document.content,
                                content_hash,
                                source_ids_json,
                                document.starts_at,
                                document.ends_at,
                                now,
                                now,
                            ),
                        )
                        changed += 1
                        continue
                    same_hash = str(existing_row["content_sha256"]) == content_hash
                    reusable = (
                        same_hash
                        and existing_row["vector"] is not None
                        and int(existing_row["dimensions"] or 0)
                        == int(space["dimensions"])
                    )
                    next_state = "ready" if reusable else "pending"
                    if (
                        existing_row["state"] != next_state
                        or not same_hash
                        or str(existing_row["parent_id"]) != document.parent_id
                        or str(existing_row["source_ids_json"]) != source_ids_json
                    ):
                        self._db.execute(
                            """UPDATE semantic_documents
                               SET parent_id=?, content=?, content_sha256=?,
                                   source_ids_json=?, starts_at=?, ends_at=?, state=?,
                                   vector=CASE WHEN ? THEN vector ELSE NULL END,
                                   dimensions=CASE WHEN ? THEN dimensions ELSE NULL END,
                                   attempts=CASE WHEN ? THEN attempts ELSE 0 END,
                                   retry_at=NULL, last_error=NULL, updated_at=?
                               WHERE space_id=? AND document_type=? AND source_id=?
                                 AND chunk_index=?""",
                            (
                                document.parent_id,
                                document.content,
                                content_hash,
                                source_ids_json,
                                document.starts_at,
                                document.ends_at,
                                next_state,
                                reusable,
                                reusable,
                                reusable,
                                now,
                                space_id,
                                *key,
                            ),
                        )
                        changed += 1
            self._db.execute(
                """DELETE FROM semantic_dirty_sources
                   WHERE source_type=? AND source_id=? AND changed_at=?""",
                (source_type, source_id, changed_at),
            )
        return changed

    def fail_semantic_source(self, claim: dict[str, object], error: Exception) -> None:
        attempts = int(
            self._db.execute(
                """SELECT attempts FROM semantic_dirty_sources
                   WHERE source_type=? AND source_id=?""",
                (claim["source_type"], claim["source_id"]),
            ).fetchone()["attempts"]
        )
        delay = min(300.0, 2.0 ** min(attempts, 8))
        with self._db:
            self._db.execute(
                """UPDATE semantic_dirty_sources
                   SET claimed_at=NULL, retry_at=?, last_error=?
                   WHERE source_type=? AND source_id=? AND changed_at=?""",
                (
                    time.time() + delay,
                    f"{type(error).__name__}: {str(error)[:240]}",
                    claim["source_type"],
                    claim["source_id"],
                    claim["changed_at"],
                ),
            )

    def claim_semantic_documents(
        self, space_id: str, limit: int
    ) -> list[dict[str, object]]:
        now = time.time()
        with self._db:
            rows = self._db.execute(
                """SELECT * FROM semantic_documents
                   WHERE space_id=? AND state IN ('pending','retry')
                     AND COALESCE(retry_at, 0)<=?
                   ORDER BY updated_at LIMIT ?""",
                (space_id, now, max(1, limit)),
            ).fetchall()
            claimed = []
            for row in rows:
                cursor = self._db.execute(
                    """UPDATE semantic_documents
                       SET state='encoding', attempts=attempts+1, updated_at=?
                       WHERE space_id=? AND document_type=? AND source_id=?
                         AND chunk_index=? AND content_sha256=?
                         AND state IN ('pending','retry')""",
                    (
                        now,
                        space_id,
                        row["document_type"],
                        row["source_id"],
                        row["chunk_index"],
                        row["content_sha256"],
                    ),
                )
                if cursor.rowcount:
                    claimed.append(dict(row))
        return claimed

    def finish_semantic_documents(
        self,
        rows: list[dict[str, object]],
        vectors: list[Iterable[float]],
        dimensions: int,
    ) -> list[tuple[str, str, int]]:
        if len(rows) != len(vectors):
            raise ValueError("embedding response count mismatch")
        now = time.time()
        updated: list[tuple[str, str, int]] = []
        with self._db:
            for row, vector in zip(rows, vectors, strict=True):
                blob = encode_vector(vector, dimensions)
                source_type = (
                    "episode"
                    if str(row["document_type"]).startswith("episode_")
                    else str(row["document_type"])
                )
                source_id = str(row["parent_id"] or row["source_id"])
                dirty = self._db.execute(
                    """SELECT 1 FROM semantic_dirty_sources
                       WHERE source_type=? AND source_id=?""",
                    (source_type, source_id),
                ).fetchone()
                if dirty is not None:
                    continue
                cursor = self._db.execute(
                    """UPDATE semantic_documents
                       SET state='ready', vector=?, dimensions=?, retry_at=NULL,
                           last_error=NULL, embedded_at=?, updated_at=?
                       WHERE space_id=? AND document_type=? AND source_id=?
                         AND chunk_index=? AND content_sha256=? AND state='encoding'""",
                    (
                        blob,
                        dimensions,
                        now,
                        now,
                        row["space_id"],
                        row["document_type"],
                        row["source_id"],
                        row["chunk_index"],
                        row["content_sha256"],
                    ),
                )
                if cursor.rowcount:
                    updated.append(
                        (
                            str(row["document_type"]),
                            str(row["source_id"]),
                            int(row["chunk_index"]),
                        )
                    )
        return updated

    def fail_semantic_documents(
        self, rows: list[dict[str, object]], error: Exception
    ) -> None:
        now = time.time()
        with self._db:
            for row in rows:
                attempts = int(row.get("attempts") or 0) + 1
                delay = min(300.0, 2.0 ** min(attempts, 8))
                self._db.execute(
                    """UPDATE semantic_documents
                       SET state='retry', retry_at=?, last_error=?, updated_at=?
                       WHERE space_id=? AND document_type=? AND source_id=?
                         AND chunk_index=? AND content_sha256=? AND state='encoding'""",
                    (
                        now + delay,
                        f"{type(error).__name__}: {str(error)[:240]}",
                        now,
                        row["space_id"],
                        row["document_type"],
                        row["source_id"],
                        row["chunk_index"],
                        row["content_sha256"],
                    ),
                )

    def semantic_ready_documents(
        self, space_id: str, *, page_size: int = 512
    ) -> Iterable[list[dict[str, object]]]:
        offset = 0
        while True:
            rows = self._db.execute(
                """SELECT document_type, source_id, parent_id, chunk_index,
                          starts_at, ends_at, vector, dimensions, content_sha256
                   FROM semantic_documents
                   WHERE space_id=? AND state='ready'
                   ORDER BY document_type, source_id, chunk_index LIMIT ? OFFSET ?""",
                (space_id, page_size, offset),
            ).fetchall()
            if not rows:
                break
            yield [dict(row) for row in rows]
            offset += len(rows)

    def semantic_ready_source_documents(
        self, space_id: str, source_type: str, source_id: str
    ) -> list[dict[str, object]]:
        if source_type == "episode":
            rows = self._db.execute(
                """SELECT document_type, source_id, parent_id, chunk_index,
                          starts_at, ends_at, vector, dimensions, content_sha256
                   FROM semantic_documents
                   WHERE space_id=? AND state='ready'
                     AND (parent_id=? OR document_type='episode_summary' AND source_id=?)
                   ORDER BY document_type, source_id, chunk_index""",
                (space_id, source_id, source_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT document_type, source_id, parent_id, chunk_index,
                          starts_at, ends_at, vector, dimensions, content_sha256
                   FROM semantic_documents
                   WHERE space_id=? AND state='ready'
                     AND document_type=? AND source_id=?
                   ORDER BY chunk_index""",
                (space_id, source_type, source_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def invalidate_semantic_document(
        self,
        space_id: str,
        document_type: str,
        source_id: str,
        chunk_index: int,
        error: str,
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE semantic_documents
                   SET state='retry', vector=NULL, dimensions=NULL, retry_at=0,
                       last_error=?, updated_at=?
                   WHERE space_id=? AND document_type=? AND source_id=?
                     AND chunk_index=?""",
                (
                    error[:240],
                    time.time(),
                    space_id,
                    document_type,
                    source_id,
                    chunk_index,
                ),
            )

    def semantic_status(self, space_id: str | None = None) -> dict[str, object]:
        if space_id is None:
            row = self._db.execute(
                """SELECT * FROM semantic_spaces
                   ORDER BY CASE state WHEN 'active' THEN 0 WHEN 'building' THEN 1 ELSE 2 END,
                            created_at DESC LIMIT 1"""
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT * FROM semantic_spaces WHERE id=?", (space_id,)
            ).fetchone()
        if row is None:
            return {"state": "absent", "eligible_source_coverage": 0.0}
        counts = {
            str(item["state"]): int(item["count"])
            for item in self._db.execute(
                """SELECT state, COUNT(*) AS count FROM semantic_documents
                   WHERE space_id=? GROUP BY state""",
                (row["id"],),
            )
        }
        eligible = self._eligible_source_ids()
        total_sources = sum(len(values) for values in eligible.values())
        covered = 0
        for source_type, source_ids in eligible.items():
            if not source_ids:
                continue
            if source_type == "episode":
                rows = self._db.execute(
                    """SELECT DISTINCT CASE WHEN document_type='episode_turn'
                                             THEN parent_id ELSE source_id END AS id
                       FROM semantic_documents WHERE space_id=?
                         AND document_type IN ('episode_summary','episode_turn')
                         AND state='ready'""",
                    (row["id"],),
                ).fetchall()
            else:
                rows = self._db.execute(
                    """SELECT DISTINCT source_id AS id FROM semantic_documents
                       WHERE space_id=? AND document_type=? AND state='ready'""",
                    (row["id"], source_type),
                ).fetchall()
            covered += len(source_ids.intersection(str(item["id"]) for item in rows))
        dirty = self._db.execute(
            "SELECT COUNT(*) AS count, MIN(changed_at) AS oldest FROM semantic_dirty_sources"
        ).fetchone()
        return {
            **dict(row),
            **{
                state: counts.get(state, 0)
                for state in ("ready", "pending", "encoding", "retry", "inactive")
            },
            "eligible_sources": total_sources,
            "covered_sources": covered,
            "eligible_source_coverage": covered / total_sources
            if total_sources
            else 1.0,
            "dirty_sources": int(dirty["count"]),
            "oldest_dirty_at": dirty["oldest"],
        }
