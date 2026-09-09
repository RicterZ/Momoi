import tempfile
import unittest
from pathlib import Path

from momoi.runtime.context.rendering import _episode_context, _episode_header
from momoi.runtime.context.retrieval import build_plan_retrieval
from momoi.semantic.models import DenseEpisodeHit, DenseRecallEvidence
from momoi.storage import Store
from momoi.storage.episode_ranking import EpisodeRecallQuery
from tests.test_episode_annealing import config


class EpisodeConfidenceTest(unittest.TestCase):
    def test_dense_only_retrieval_exposes_its_evidence_strength(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "db.sqlite3")
            store.create_episode("旧日准备", episode_id="past")
            query = EpisodeRecallQuery("", semantic_expression="之前的面试练习")
            evidence = DenseRecallEvidence(
                calibration_profile="bge-small-zh-v1.5-momoi-v1",
                episodes={
                    query.dense_expression: {
                        "past": DenseEpisodeHit("past", summary_cosine=0.83),
                    }
                },
            )
            rows = store.search_episode_queries([query], 8, dense_evidence=evidence)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["dense_only"])
            self.assertGreater(rows[0]["relevance_confidence"], 0.9)
            selected = {
                "episode_id": "past",
                "relevance_confidence": rows[0]["relevance_confidence"],
            }
            rendered = _episode_context(store, [selected], 1000)
            self.assertIn('confidence="0.969"', rendered)
            self.assertNotIn(
                "confidence=", _episode_context(store, [{"episode_id": "past"}], 1000)
            )
            store.close()

    def test_unknown_or_invalid_confidence_is_not_fabricated(self):
        episode = {"id": "past", "status": "closed"}
        for value in (None, float("nan"), float("inf"), -1, 2, True, "0.8"):
            with self.subTest(value=value):
                self.assertNotIn(
                    "confidence=",
                    _episode_header(episode, {"relevance_confidence": value}),
                )
        self.assertIn(
            'confidence="0.000"', _episode_header(episode, {"relevance_confidence": 0})
        )

    def test_plan_retrieval_preserves_query_confidence_into_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "db.sqlite3")
            store.create_episode("模拟面试", episode_id="past")
            plan = {
                "intent_units": [
                    {
                        "id": "u1",
                        "text": "那次模拟面试",
                        "intent": "conversation",
                        "recall_mode": "search",
                        "recall_queries": [
                            {"semantic": "模拟面试", "keywords": ["模拟面试"]}
                        ],
                        "recall_from_turn_id": "",
                        "episode": {"action": "none"},
                    }
                ]
            }
            retrieval = build_plan_retrieval(store, plan, config(directory))
            selected = next(
                r for r in retrieval["episodes"] if r["episode_id"] == "past"
            )
            self.assertGreater(selected["relevance_confidence"], 0)
            self.assertIn("confidence=", _episode_context(store, [selected], 1000))
            store.close()
