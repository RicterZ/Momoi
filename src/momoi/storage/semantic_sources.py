import json
import sqlite3
import time

from .semantic_documents import (
    SemanticDocument,
    _episode_summary_document,
    _episode_turn_documents,
)


class SemanticSourceStore:
    _db: sqlite3.Connection

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
