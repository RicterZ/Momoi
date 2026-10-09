"""Read and invalidate indexed vectors using the caller-owned SQLite connection."""
import sqlite3
import time
from typing import Iterable

from .transactions import transaction


class VectorRepository:
    def __init__(self, database: sqlite3.Connection) -> None:
        self._db = database

    def ready_documents(
        self, space_id: str, *, page_size: int = 512
    ) -> Iterable[list[dict[str, object]]]:
        page_size = max(1, page_size)
        last_key = ("", "", -1)
        while True:
            rows = self._db.execute(
                """SELECT document_type, source_id, parent_id, chunk_index,
                          starts_at, ends_at, vector, dimensions, content_sha256
                   FROM semantic_documents
                   WHERE space_id=? AND state='ready'
                     AND (document_type, source_id, chunk_index) > (?, ?, ?)
                   ORDER BY document_type, source_id, chunk_index LIMIT ?""",
                (space_id, *last_key, page_size),
            ).fetchall()
            if not rows:
                break
            last = rows[-1]
            last_key = (last["document_type"], last["source_id"], last["chunk_index"])
            # Invalid vectors can leave the ready set while the caller consumes a page.
            yield [dict(row) for row in rows]

    def ready_source_documents(
        self, space_id: str, document_type: str, source_id: str, *, include_children: bool = False,
    ) -> list[dict[str, object]]:
        if include_children:
            rows = self._db.execute(
                """SELECT document_type, source_id, parent_id, chunk_index,
                          starts_at, ends_at, vector, dimensions, content_sha256
                   FROM semantic_documents
                   WHERE space_id=? AND state='ready'
                     AND (parent_id=? OR document_type=? AND source_id=?)
                   ORDER BY document_type, source_id, chunk_index""",
                (space_id, source_id, document_type, source_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT document_type, source_id, parent_id, chunk_index,
                          starts_at, ends_at, vector, dimensions, content_sha256
                   FROM semantic_documents
                   WHERE space_id=? AND state='ready'
                     AND document_type=? AND source_id=?
                   ORDER BY chunk_index""",
                (space_id, document_type, source_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def invalidate(
        self,
        space_id: str,
        document_type: str,
        source_id: str,
        chunk_index: int,
        error: str,
    ) -> None:
        with transaction(self._db):
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
