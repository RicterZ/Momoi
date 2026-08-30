import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from momoi.config import EmbeddingConfig
from momoi.search import StringSearchBackend
from momoi.semantic import (
    DenseEpisodeHit,
    DenseMemoryHit,
    DenseRecallEvidence,
    SemanticRecallService,
)
from momoi.storage import MemoryRecallQuery, Store, encode_vector
from momoi.storage.episode_ranking import EpisodeRecallQuery, rank_episode_matches
from momoi.storage.episode_search import (
    EpisodeQueryService,
    EpisodeSearchDocument,
    StringEpisodeSearchBackend,
)


def vector(first: float = 1.0, second: float = 0.0) -> list[float]:
    value = [0.0] * 512
    value[0] = first
    value[1] = second
    return value


class SemanticRecallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "momoi.sqlite3")
        self.now = time.time()

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def add_memory(self, key: str, content: str, *, importance: float = 0.5) -> int:
        with self.store._db:
            cursor = self.store._db.execute(
                """INSERT INTO memories
                   (kind, key, content, activation, authority, source_event_id,
                    evidence_quote, importance, created_at, updated_at)
                   VALUES ('shared', ?, ?, 'recall', 'owner', 'event', ?, ?, ?, ?)""",
                (key, content, content, importance, self.now, self.now),
            )
        return int(cursor.lastrowid)

    def add_reflection(self, key: str, content: str, confidence: float = 0.7) -> int:
        with self.store._db:
            self.store._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, summary, created_at)
                   VALUES ('reflection', '2026-08-30', 'completed', ?, '', ?)""",
                (self.now, self.now),
            )
            cursor = self.store._db.execute(
                """INSERT INTO reflection_memories
                   (kind, key, content, evidence, confidence,
                    source_reflection_id, created_at, updated_at)
                   VALUES ('practice', ?, ?, 'evidence', ?, 'reflection', ?, ?)""",
                (key, content, confidence, self.now, self.now),
            )
        return int(cursor.lastrowid)

    def space(self, *, state: str = "building") -> dict[str, object]:
        return self.store.ensure_semantic_space(
            model="BAAI/bge-small-zh-v1.5",
            dimensions=512,
            calibration_profile="bge-small-zh-v1.5-momoi-v1",
            state=state,
        )

    def materialize_all(self) -> None:
        while claims := self.store.claim_semantic_sources(100):
            for claim in claims:
                self.store.materialize_semantic_source(claim)

    def test_memory_and_reflection_lifecycle_are_independent(self) -> None:
        memory_id = self.add_memory("device", "turn on the device")
        reflection_id = self.add_reflection("method", "use the direct control tool")
        space = self.space()
        self.store.reconcile_semantic_sources(str(space["id"]))
        self.materialize_all()
        rows = self.store._db.execute(
            """SELECT document_type, source_id, state FROM semantic_documents
               WHERE space_id=? ORDER BY document_type""",
            (space["id"],),
        ).fetchall()
        self.assertEqual(
            [(row["document_type"], row["source_id"], row["state"]) for row in rows],
            [
                ("confirmed_memory", str(memory_id), "pending"),
                ("reflection_memory", str(reflection_id), "pending"),
            ],
        )
        with self.store._db:
            self.store._db.execute(
                "UPDATE memories SET activation='always' WHERE id=?", (memory_id,)
            )
            self.store._db.execute(
                "DELETE FROM reflection_memories WHERE id=?", (reflection_id,)
            )
        self.materialize_all()
        count = self.store._db.execute(
            "SELECT COUNT(*) AS count FROM semantic_documents WHERE space_id=?",
            (space["id"],),
        ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_source_change_during_encoding_rejects_old_vector(self) -> None:
        memory_id = self.add_memory("device", "old procedure")
        space = self.space()
        self.store.reconcile_semantic_sources(str(space["id"]))
        self.materialize_all()
        rows = self.store.claim_semantic_documents(str(space["id"]), 8)
        self.assertEqual(len(rows), 1)
        with self.store._db:
            self.store._db.execute(
                "UPDATE memories SET content='new procedure' WHERE id=?", (memory_id,)
            )
        updated = self.store.finish_semantic_documents(rows, [vector()], 512)
        self.assertEqual(updated, [])
        state = self.store._db.execute(
            "SELECT state FROM semantic_documents WHERE space_id=?",
            (space["id"],),
        ).fetchone()["state"]
        self.assertEqual(state, "encoding")
        self.materialize_all()
        row = self.store._db.execute(
            "SELECT state, content FROM semantic_documents WHERE space_id=?",
            (space["id"],),
        ).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertIn("new procedure", row["content"])

    def test_restart_recovers_encoding_without_rebuilding_ready(self) -> None:
        self.add_memory("device", "procedure")
        space = self.space()
        self.store.reconcile_semantic_sources(str(space["id"]))
        self.materialize_all()
        rows = self.store.claim_semantic_documents(str(space["id"]), 8)
        self.store.finish_semantic_documents(rows, [vector()], 512)
        embedded_at = self.store._db.execute(
            "SELECT embedded_at FROM semantic_documents"
        ).fetchone()["embedded_at"]
        self.store.reconcile_semantic_sources(str(space["id"]))
        self.assertEqual(self.store.claim_semantic_sources(10), [])
        self.assertEqual(self.store.claim_semantic_documents(str(space["id"]), 8), [])
        self.assertEqual(
            self.store._db.execute(
                "SELECT embedded_at FROM semantic_documents"
            ).fetchone()["embedded_at"],
            embedded_at,
        )

    def test_restart_recovery_does_not_dirty_unchanged_episodes(self) -> None:
        episode_id = "episode-1"
        with self.store._db:
            self.store._db.execute(
                """INSERT INTO conversation_episodes
                   (id, status, title, narrative_summary,
                    summarized_through_ordinal, summary_claimed_at,
                    created_at, updated_at, closed_at)
                   VALUES (?, 'closed', 'topic', 'summary', 0, ?, ?, ?, ?)""",
                (episode_id, self.now, self.now, self.now, self.now),
            )
        space = self.space()
        self.store.reconcile_semantic_sources(str(space["id"]))
        self.materialize_all()
        self.assertEqual(
            self.store.semantic_status(str(space["id"]))["dirty_sources"], 0
        )

        database = Path(self.directory.name) / "momoi.sqlite3"
        self.store.close()
        self.store = Store(database)

        self.assertEqual(
            self.store.semantic_status(str(space["id"]))["dirty_sources"], 0
        )

    def test_dense_only_requires_absolute_threshold(self) -> None:
        memory_id = self.add_memory("unrelated literal", "stored paraphrase")
        query = MemoryRecallQuery("different wording")
        low = DenseRecallEvidence(
            calibration_profile="bge-small-zh-v1.5-momoi-v1",
            memory={
                query.expression: {
                    ("confirmed_memory", str(memory_id)): DenseMemoryHit(
                        str(memory_id), 0.71
                    )
                }
            },
        )
        high = DenseRecallEvidence(
            calibration_profile="bge-small-zh-v1.5-momoi-v1",
            memory={
                query.expression: {
                    ("confirmed_memory", str(memory_id)): DenseMemoryHit(
                        str(memory_id), 0.80
                    )
                }
            },
        )
        self.assertEqual(
            self.store.rank_recalled_memories([query], 6, dense_evidence=low), []
        )
        selected = self.store.rank_recalled_memories([query], 6, dense_evidence=high)
        self.assertEqual([row["id"] for row in selected], [memory_id])
        self.assertTrue(selected[0]["dense_only"])

    def test_query_priority_does_not_veto_dense_eligibility(self) -> None:
        memory_id = self.add_memory("stored", "semantic relation")
        query = MemoryRecallQuery("paraphrase", priority=2)
        evidence = DenseRecallEvidence(
            calibration_profile="bge-small-zh-v1.5-momoi-v1",
            memory={
                query.expression: {
                    ("confirmed_memory", str(memory_id)): DenseMemoryHit(
                        str(memory_id), 0.80
                    )
                }
            },
        )
        selected = self.store.rank_recalled_memories(
            [query], 6, dense_evidence=evidence
        )
        self.assertEqual([row["id"] for row in selected], [memory_id])

    def test_sparse_fallback_preserves_result_order(self) -> None:
        first = self.add_memory("exact", "first", importance=0.9)
        second = self.add_memory("exact", "second", importance=0.1)
        query = MemoryRecallQuery("exact")
        baseline = self.store.rank_recalled_memories([query], 6)
        fallback = self.store.rank_recalled_memories(
            [query],
            6,
            dense_evidence=DenseRecallEvidence(
                calibration_profile="bge-small-zh-v1.5-momoi-v1",
                fallback_reason="offline",
            ),
        )
        self.assertEqual([row["id"] for row in baseline], [first, second])
        self.assertEqual(
            [(row["id"], row["search_score"]) for row in baseline],
            [(row["id"], row["search_score"]) for row in fallback],
        )

    def test_sparse_search_uses_only_literal_keywords(self) -> None:
        semantic_only = self.add_memory(
            "semantic phrase", "老师此前如何处理客厅设备"
        )
        keyword_hit = self.add_memory("device-42", "设备的真实编号")
        query = MemoryRecallQuery(
            "device-42",
            semantic_expression="老师此前如何处理客厅设备",
        )

        selected = self.store.rank_recalled_memories([query], 6)

        self.assertEqual([row["id"] for row in selected], [keyword_hit])
        self.assertNotIn(semantic_only, [row["id"] for row in selected])

    def test_empty_sparse_keywords_still_allow_dense_recall(self) -> None:
        memory_id = self.add_memory("literal absent", "stored semantic answer")
        query = MemoryRecallQuery(
            "",
            semantic_expression="老师对自动操作的长期偏好",
        )
        evidence = DenseRecallEvidence(
            calibration_profile="bge-small-zh-v1.5-momoi-v1",
            memory={
                query.dense_expression: {
                    ("confirmed_memory", str(memory_id)): DenseMemoryHit(
                        str(memory_id), 0.80
                    )
                }
            },
        )

        selected = self.store.rank_recalled_memories(
            [query], 6, dense_evidence=evidence
        )

        self.assertEqual([row["id"] for row in selected], [memory_id])
        self.assertEqual(selected[0]["channels"], ["dense"])
        self.assertEqual(
            selected[0]["matched_queries"], ["老师对自动操作的长期偏好"]
        )

    def test_sparse_dense_agreement_adds_an_explainable_bonus(self) -> None:
        memory_id = self.add_memory("exact", "stored fact")
        query = MemoryRecallQuery("exact")
        baseline = self.store.rank_recalled_memories([query], 6)[0]
        evidence = DenseRecallEvidence(
            calibration_profile="bge-small-zh-v1.5-momoi-v1",
            memory={
                query.expression: {
                    ("confirmed_memory", str(memory_id)): DenseMemoryHit(
                        str(memory_id), 0.80
                    )
                }
            },
        )
        hybrid = self.store.rank_recalled_memories([query], 6, dense_evidence=evidence)[
            0
        ]
        self.assertGreater(hybrid["search_score"], baseline["search_score"])
        self.assertGreater(hybrid["agreement_bonus"], 0)
        self.assertEqual(hybrid["channels"], ["dense", "sparse"])

    def test_episode_summary_and_turn_corroborate_once(self) -> None:
        documents = [
            EpisodeSearchDocument("long", (), 100.0, 0.5, ()),
            EpisodeSearchDocument("short", (), 100.0, 0.5, ()),
        ]
        query = EpisodeRecallQuery("paraphrase")
        matches = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        ).match_many([query.expression], documents)
        evidence = DenseRecallEvidence(
            calibration_profile="bge-small-zh-v1.5-momoi-v1",
            episodes={
                query.expression: {
                    "long": DenseEpisodeHit(
                        "long", summary_cosine=0.82, turn_cosine=0.82
                    ),
                    "short": DenseEpisodeHit("short", summary_cosine=0.82),
                }
            },
        )
        ranked = rank_episode_matches(
            [query], matches, documents, dense_evidence=evidence, now=100.0
        )
        self.assertEqual([hit.episode_id for hit in ranked], ["long", "short"])
        self.assertGreater(ranked[0].corroboration_bonus, 0)
        self.assertEqual(ranked[1].corroboration_bonus, 0)

    def test_episode_reopen_hides_vectors_and_reclose_reuses_them(self) -> None:
        episode_id = "episode-1"
        turn_id = "turn-1"
        with self.store._db:
            self.store._db.execute(
                """INSERT INTO turns
                   (id, kind, source_ids_json, state, started_at, updated_at)
                   VALUES (?, 'owner', '[]', 'completed', ?, ?)""",
                (turn_id, self.now, self.now),
            )
            self.store._db.execute(
                """INSERT INTO conversation_episodes
                   (id, status, title, narrative_summary,
                    summarized_through_ordinal, created_at, updated_at, closed_at)
                   VALUES (?, 'closed', 'topic', 'summary', 1, ?, ?, ?)""",
                (episode_id, self.now, self.now, self.now),
            )
            self.store._db.execute(
                """INSERT INTO episode_turns
                   (episode_id, turn_id, ordinal, relation)
                   VALUES (?, ?, 1, 'primary')""",
                (episode_id, turn_id),
            )
            self.store._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES (?, 'user', 'owner history', ?, '[]')""",
                (turn_id, self.now),
            )
        space = self.space()
        self.store.reconcile_semantic_sources(str(space["id"]))
        self.materialize_all()
        rows = self.store.claim_semantic_documents(str(space["id"]), 8)
        self.store.finish_semantic_documents(rows, [vector()] * len(rows), 512)
        self.assertGreater(len(rows), 1)
        with self.store._db:
            self.store._db.execute(
                "UPDATE conversation_episodes SET status='open' WHERE id=?",
                (episode_id,),
            )
        self.materialize_all()
        states = {
            row["state"]
            for row in self.store._db.execute(
                "SELECT state FROM semantic_documents WHERE space_id=?",
                (space["id"],),
            )
        }
        self.assertEqual(states, {"inactive"})
        with self.store._db:
            self.store._db.execute(
                "UPDATE conversation_episodes SET status='closed' WHERE id=?",
                (episode_id,),
            )
        self.materialize_all()
        states = {
            row["state"]
            for row in self.store._db.execute(
                "SELECT state FROM semantic_documents WHERE space_id=?",
                (space["id"],),
            )
        }
        self.assertEqual(states, {"ready"})

    def test_confirmed_and_reflection_keep_separate_limits(self) -> None:
        confirmed = [self.add_memory("exact", f"memory {index}") for index in range(8)]
        reflected = [
            self.add_reflection(f"exact {index}", f"reflection {index}")
            for index in range(8)
        ]
        rows = self.store.rank_recalled_memories([MemoryRecallQuery("exact")], 6)
        self.assertEqual(sum(row["source"] == "confirmed" for row in rows), 6)
        self.assertEqual(sum(row["source"] == "reflection" for row in rows), 6)
        self.assertTrue(
            set(row["id"] for row in rows if row["source"] == "confirmed").issubset(
                confirmed
            )
        )
        self.assertTrue(
            set(row["id"] for row in rows if row["source"] == "reflection").issubset(
                reflected
            )
        )

    def test_new_space_cannot_activate_before_coverage(self) -> None:
        self.add_memory("device", "procedure")
        space = self.space()
        self.store.reconcile_semantic_sources(str(space["id"]))
        with self.assertRaises(ValueError):
            self.store.activate_semantic_space(str(space["id"]))

    def test_time_scoped_dense_search_excludes_episode_summary(self) -> None:
        episode_id = "episode-1"
        space = self.space(state="active")
        with self.store._db:
            for document_type, source_id, parent_id, starts_at, ends_at, raw in (
                ("episode_summary", episode_id, episode_id, None, None, vector(1, 0)),
                ("episode_turn", "turn-1", episode_id, 100.0, 110.0, vector(0, 1)),
            ):
                self.store._db.execute(
                    """INSERT INTO semantic_documents
                       (space_id, document_type, source_id, parent_id, chunk_index,
                        content, content_sha256, starts_at, ends_at, state,
                        vector, dimensions, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 0, 'x', 'hash', ?, ?, 'ready', ?, 512, ?, ?)""",
                    (
                        space["id"],
                        document_type,
                        source_id,
                        parent_id,
                        starts_at,
                        ends_at,
                        encode_vector(raw, 512),
                        self.now,
                        self.now,
                    ),
                )
        service = SemanticRecallService(self.store, EmbeddingConfig(enabled=True))
        service.start()

        async def run() -> DenseRecallEvidence:
            async def encode(_texts: list[str], *, query: bool) -> list[list[float]]:
                return [vector(1, 0)]

            service.client.encode = encode  # type: ignore[method-assign]
            try:
                return await service.prepare(
                    [EpisodeRecallQuery("query")],
                    include_memory=False,
                    episode_after=90,
                    episode_before=120,
                )
            finally:
                await service.close()

        evidence = asyncio.run(run())
        hit = evidence.episodes["query"][episode_id]
        self.assertIsNone(hit.summary_cosine)
        self.assertAlmostEqual(hit.turn_cosine or 0, 0.0, places=5)

    def test_dense_query_uses_semantic_rewrite_once_not_sparse_aliases(self) -> None:
        memory_id = self.add_memory("stored", "semantic")
        space = self.space(state="active")
        with self.store._db:
            self.store._db.execute(
                """INSERT INTO semantic_documents
                   (space_id, document_type, source_id, chunk_index, content,
                    content_sha256, state, vector, dimensions, created_at, updated_at)
                   VALUES (?, 'confirmed_memory', ?, 0, 'x', 'hash', 'ready', ?, 512, ?, ?)""",
                (
                    space["id"],
                    str(memory_id),
                    encode_vector(vector(), 512),
                    self.now,
                    self.now,
                ),
            )
        service = SemanticRecallService(self.store, EmbeddingConfig(enabled=True))
        service.start()

        async def run() -> tuple[float, list[list[str]]]:
            encoded_batches: list[list[str]] = []

            async def encode(texts: list[str], *, query: bool) -> list[list[float]]:
                encoded_batches.append(texts)
                return [vector(0.8, 0.6) for _text in texts]

            service.client.encode = encode  # type: ignore[method-assign]
            try:
                evidence = await service.prepare(
                    [
                        MemoryRecallQuery(
                            "alpha|beta",
                            semantic_expression="historical alpha and beta relationship",
                        )
                    ],
                    include_episode=False,
                )
                return evidence.memory["historical alpha and beta relationship"][
                    ("confirmed_memory", str(memory_id))
                ].cosine, encoded_batches
            finally:
                await service.close()

        cosine, encoded_batches = asyncio.run(run())
        self.assertAlmostEqual(cosine, 0.8)
        self.assertEqual(len(encoded_batches), 1)
        self.assertEqual(len(encoded_batches[0]), 1)
        self.assertTrue(
            encoded_batches[0][0].endswith("historical alpha and beta relationship")
        )
        self.assertNotIn("alpha|beta", encoded_batches[0][0])


if __name__ == "__main__":
    unittest.main()
