import sqlite3
import time
from typing import Iterable

from .semantic_documents import encode_vector


class SemanticQueueStore:
    _db: sqlite3.Connection

    def recover_semantic_encoding(self) -> int:
        with self._db:
            cursor = self._db.execute(
                """UPDATE semantic_documents
                   SET state='pending', retry_at=NULL, last_error='worker_restarted'
                   WHERE state='encoding'"""
            )
        return cursor.rowcount

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

