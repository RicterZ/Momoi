import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from momoi.search import StringSearchBackend
from momoi.storage import Store
from momoi.storage.episode_ranking import EpisodeRecallQuery, rank_episode_matches
from momoi.storage.episode_search import (
    EpisodeQueryService,
    EpisodeSearchField,
    StringEpisodeSearchBackend,
)
from momoi.storage.migrations import SCHEMA_VERSION
from momoi.storage.semantic_documents import _episode_summary_document, _episode_cue_documents
from tests.test_episode_search import document
from tests.test_episode_annealing import add_turn, config
from momoi.runtime import MomoiDaemon


class EpisodeCuesTest(unittest.TestCase):
    def rank(self, documents, query):
        service = EpisodeQueryService(StringEpisodeSearchBackend(StringSearchBackend()))
        return rank_episode_matches(
            [EpisodeRecallQuery(query)],
            service.match_many([query], documents),
            documents,
            now=100,
        )

    def test_cue_recovers_paraphrase_and_beats_incidental_summary(self):
        target = document("practice", title="周二晚上的准备")
        distractor = replace(
            document("other"),
            fields=(EpisodeSearchField("narrative_summary", "下次可以模拟面试"),),
        )
        self.assertNotIn(
            "practice",
            [h.episode_id for h in self.rank([target, distractor], "模拟面试")],
        )
        target = replace(
            target,
            fields=target.fields + (EpisodeSearchField("recall_cue", "一起模拟面试"),),
        )
        hits = self.rank([target, distractor], "模拟面试")
        self.assertEqual([h.episode_id for h in hits], ["practice", "other"])
        self.assertIn("recall_cue", hits[0].matched_queries[0].field_matches)

    def test_repeated_cue_matches_do_not_inflate_score(self):
        cue = EpisodeSearchField("recall_cue", "一起模拟面试")
        single = replace(document("same"), fields=(cue,))
        repeated = replace(
            single, fields=(cue, cue, EpisodeSearchField("recall_cue", "模拟面试练习"))
        )
        self.assertEqual(
            self.rank([single], "模拟面试")[0].score,
            self.rank([repeated], "模拟面试")[0].score,
        )
        self.assertEqual(self.rank([repeated], "面试"), [])

    def test_multivalued_fields_each_contribute_one_signal(self):
        for name in ("title", "topic", "entity", "open_loop", "recall_cue"):
            with self.subTest(field=name):
                field = EpisodeSearchField(name, "模拟面试")
                single = replace(document("same"), fields=(field,))
                repeated = replace(single, fields=(field, field, field))
                one = self.rank([single], "模拟面试")[0]
                many = self.rank([repeated], "模拟面试")[0]
                self.assertEqual(one.score, many.score)
                self.assertEqual(one.relevance_confidence, many.relevance_confidence)

    def test_cue_label_receives_calibrated_premium_over_title(self):
        scores = []
        for name in ("title", "recall_cue"):
            item = replace(
                document("same"), fields=(EpisodeSearchField(name, "模拟面试"),)
            )
            scores.append(self.rank([item], "模拟面试")[0].score)
        self.assertGreater(scores[1], scores[0])

    def test_summary_persists_indexes_replaces_and_validates_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            store = daemon.store
            store.create_episode("练习", episode_id="episode-main")
            add_turn(daemon, 1)
            row = store._db.execute(
                "SELECT id, content FROM messages WHERE role='user'"
            ).fetchone()
            claims = [
                dict(
                    message_id=row["id"],
                    turn_id="turn-1",
                    ordinal=1,
                    quote=row["content"],
                )
            ]

            def finish(cues):
                store._db.execute(
                    "UPDATE conversation_episodes SET summary_claimed_at=1 WHERE id='episode-main'"
                )
                return store.finish_episode_annealing(
                    "episode-main", 1, claims, recall_cues=cues
                )

            finish([" 初次  交流 ", "初次 交流"])
            self.assertEqual(
                store.episode("episode-main")["recall_cues"], ["初次 交流"]
            )
            self.assertEqual(
                len(store.search_episode_queries([EpisodeRecallQuery("初次 交流")], 8)),
                1,
            )
            self.assertEqual(
                store.search_episode_queries(
                    [EpisodeRecallQuery("初次 交流")], 8, after=0
                ),
                [],
            )
            terms = store._db.execute(
                "SELECT term FROM recall_terms JOIN episode_recall_terms ON term_id=recall_terms.id"
            ).fetchall()
            self.assertTrue(any("初次" in r[0] for r in terms))
            source = store._db.execute("SELECT * FROM conversation_episodes").fetchone()
            before = _episode_summary_document(source).content
            self.assertEqual(_episode_cue_documents(source)[0].content, "初次 交流")
            for invalid in ([""], ["x" * 101], ["x"] * 9, "phrase", [1], {}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    finish(invalid)
                self.assertEqual(
                    store.episode("episode-main")["recall_cues"], ["初次 交流"]
                )
            linked = [{"text": "有来源的线索", "evidence_message_ids": [row["id"]]}]
            finish(linked)
            self.assertEqual(store.episode("episode-main")["recall_cues"], ["有来源的线索"])
            self.assertEqual(store.episode("episode-main")["recall_cue_sources"], linked)
            for ids in ([row["id"] + 1000], [], [True]):
                with self.assertRaises(ValueError):
                    finish([{"text": "错误来源", "evidence_message_ids": ids}])
                self.assertEqual(store.episode("episode-main")["recall_cue_sources"], linked)
            store._db.execute("DELETE FROM semantic_dirty_sources")
            finish([])
            self.assertEqual(
                store.search_episode_queries([EpisodeRecallQuery("初次 交流")], 8), []
            )
            source = store._db.execute("SELECT * FROM conversation_episodes").fetchone()
            self.assertEqual(_episode_summary_document(source).content, before)
            self.assertEqual(_episode_cue_documents(source), [])
            self.assertIsNotNone(
                store._db.execute(
                    "SELECT * FROM semantic_dirty_sources WHERE source_id='episode-main'"
                ).fetchone()
            )
            store.close()

    def test_legacy_database_migrates_and_cue_only_updates_dirty_vectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            store = Store(path)
            store.create_episode("legacy", episode_id="legacy")
            store.close()
            with sqlite3.connect(path) as db:
                db.execute("DROP TRIGGER semantic_episodes_update")
                db.execute(
                    "ALTER TABLE conversation_episodes DROP COLUMN recall_cues_json"
                )
                db.execute(f"PRAGMA user_version={7}")
            for _ in range(2):
                store = Store(path)
                self.assertEqual(store.episode("legacy")["recall_cues"], [])
                store.close()
            store = Store(path)
            with store._db:
                store._db.execute("DELETE FROM semantic_dirty_sources")
                store._db.execute(
                    "UPDATE conversation_episodes SET recall_cues_json=? WHERE id='legacy'",
                    (json.dumps(["旧事线索"]),),
                )
            self.assertIsNotNone(
                store._db.execute(
                    "SELECT * FROM semantic_dirty_sources WHERE source_id='legacy'"
                ).fetchone()
            )
            store.close()
