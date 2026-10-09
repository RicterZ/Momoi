"""Materialize supplied source documents into active and building indexes."""
import json
import sqlite3
import time

from .index_records import IndexDocument
from .transactions import transaction


class IndexDocuments:
    def __init__(self, database: sqlite3.Connection) -> None:
        self._db = database

    def materialize(
        self, claim: dict[str, object], documents: list[IndexDocument], *,
        document_type: str, include_children: bool = False, keep_removed: bool = False,
    ) -> int:
        source_type = str(claim["source_type"])
        source_id = str(claim["source_id"])
        changed_at = float(claim["changed_at"])
        expected = {
            (doc.document_type, doc.source_id, doc.chunk_index): doc
            for doc in documents
        }
        now = time.time()
        changed = 0
        with transaction(self._db):
            current = self._db.execute(
                """SELECT 1 FROM semantic_dirty_sources
                   WHERE source_type=? AND source_id=? AND changed_at=?""",
                (source_type, source_id, changed_at),
            ).fetchone()
            if current is None:
                return 0
            spaces = self._db.execute(
                "SELECT id, dimensions FROM semantic_spaces WHERE state IN ('building','active')"
            ).fetchall()
            for space in spaces:
                space_id = str(space["id"])
                if include_children:
                    existing = self._db.execute(
                        """SELECT * FROM semantic_documents
                           WHERE space_id=? AND (parent_id=? OR
                               document_type=? AND source_id=?)""",
                        (space_id, source_id, document_type, source_id),
                    ).fetchall()
                else:
                    existing = self._db.execute(
                        """SELECT * FROM semantic_documents
                           WHERE space_id=? AND document_type=? AND source_id=?""",
                        (space_id, document_type, source_id),
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
                    if keep_removed:
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
