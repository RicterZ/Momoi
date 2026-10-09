import sqlite3
import time

from ...memory.storage.transactions import transaction

from ...memory.storage.index_records import IndexDocument
from .semantic_documents import _episode_summary_document, _episode_turn_documents, _episode_cue_documents


class SemanticSourceStore:
    _db: sqlite3.Connection

    def _eligible_source_ids(self) -> dict[str, set[str]]:
        confirmed = self.memories.index_source.eligible_ids()
        reflections = {
            str(row["id"])
            for row in self._db.execute(
                """SELECT id FROM reflection_memories AS rm
                   WHERE NOT EXISTS (
                       SELECT 1 FROM reflection_memory_tombstones AS t
                       WHERE t.kind=rm.kind AND t.key=rm.key
                   )"""
            )
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


    def _source_documents(
        self, source_type: str, source_id: str
    ) -> tuple[list[IndexDocument], bool]:
        if source_type == "confirmed_memory":
            documents = self.memories.index_source.documents(source_id)
            return documents, bool(documents)
        if source_type == "reflection_memory":
            row = self._db.execute(
                """SELECT rm.id, rm.kind, rm.key, rm.content
                   FROM reflection_memories AS rm
                   WHERE rm.id=?
                     AND NOT EXISTS (
                         SELECT 1 FROM reflection_memory_tombstones AS t
                         WHERE t.kind=rm.kind AND t.key=rm.key
                     )""",
                (source_id,),
            ).fetchone()
            if row is None:
                return [], False
            return [
                IndexDocument(
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
        return ([summary] if summary else []) + _episode_cue_documents(episode) + _episode_turn_documents(
            source_id, list(rows)
        ), True

    def materialize_semantic_source(self, claim: dict[str, object]) -> int:
        source_type, source_id = str(claim["source_type"]), str(claim["source_id"])
        with transaction(self._db):
            documents, exists = self._source_documents(source_type, source_id)
            return self.memory_index_documents.materialize(
                claim, documents,
                document_type="episode_summary" if source_type == "episode" else source_type,
                include_children=source_type == "episode",
                keep_removed=source_type == "episode" and exists,
            )
