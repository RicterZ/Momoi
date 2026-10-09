"""Persistent indexing claims, vector writes, and retry state."""
import sqlite3
import time
from typing import Iterable

from .vectors import encode_vector
from .transactions import transaction


class IndexQueue:
    def __init__(self, database: sqlite3.Connection) -> None:
        self._db = database

    def recover_encoding(self) -> int:
        with transaction(self._db):
            cursor = self._db.execute(
                """UPDATE semantic_documents
                   SET state='pending', retry_at=NULL, last_error='worker_restarted'
                   WHERE state='encoding'"""
            )
        return cursor.rowcount

    def claim_documents(
        self, space_id: str, limit: int
    ) -> list[dict[str, object]]:
        now = time.time()
        with transaction(self._db):
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

    def finish_documents(
        self,
        rows: list[dict[str, object]],
        vectors: list[Iterable[float]],
        dimensions: int,
        source_keys: list[tuple[str, str]],
    ) -> list[tuple[str, str, int]]:
        if len(rows) != len(vectors) or len(rows) != len(source_keys):
            raise ValueError("embedding response count mismatch")
        now = time.time()
        updated: list[tuple[str, str, int]] = []
        with transaction(self._db):
            for row, vector, (source_type, source_id) in zip(rows, vectors, source_keys, strict=True):
                blob = encode_vector(vector, dimensions)
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

    def fail_documents(
        self, rows: list[dict[str, object]], error: Exception
    ) -> None:
        now = time.time()
        with transaction(self._db):
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

    def claim_sources(self, limit: int = 16) -> list[dict[str, object]]:
        now = time.time()
        with transaction(self._db):
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

    def fail_source(self, claim: dict[str, object], error: Exception) -> None:
        row = self._db.execute(
            """SELECT attempts FROM semantic_dirty_sources
               WHERE source_type=? AND source_id=? AND changed_at=?""",
            (claim["source_type"], claim["source_id"], claim["changed_at"]),
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"])
        delay = min(300.0, 2.0 ** min(attempts, 8))
        with transaction(self._db):
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
