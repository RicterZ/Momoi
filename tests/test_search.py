import unittest

from momoi.search import StringSearchBackend, search_alternatives, search_expression


class ScoredBackend:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    def search_one(self, keyword: str, _texts: object) -> float | None:
        return self.scores.get(keyword)


class SearchContractTest(unittest.TestCase):
    def test_alternatives_remain_normalized_deduplicated_or_phrases(self) -> None:
        self.assertEqual(
            search_alternatives(" 房间 | 屋子｜ＦＯＯ | 房间 "),
            ("房间", "屋子", "foo"),
        )

    def test_expression_preserves_backend_scores_without_changing_coverage(self) -> None:
        match = search_expression(
            "房间|屋子|院子",
            ("irrelevant",),
            ScoredBackend({"房间": 0.91, "屋子": 0.55}),
        )

        self.assertIsNotNone(match)
        self.assertEqual(match.score, 2 / 3)
        self.assertEqual(match.alternatives, ("房间", "屋子"))
        self.assertEqual(match.alternative_scores, (0.91, 0.55))

    def test_string_backend_keeps_exact_substring_behavior(self) -> None:
        backend = StringSearchBackend()

        self.assertEqual(backend.search_one("ＦＯＯ", ("prefix foo suffix",)), 1.0)
        self.assertIsNone(backend.search_one("bar", ("prefix foo suffix",)))


if __name__ == "__main__":
    unittest.main()
