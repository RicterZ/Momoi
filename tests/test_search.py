import unittest

from momoi.search import (
    StringSearchBackend,
    alternative_weights,
    document_frequency,
    search_alternatives,
    search_expression,
)


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

    def test_string_backend_normalizes_literals_and_bounds_ascii_words(self) -> None:
        backend = StringSearchBackend()

        self.assertEqual(backend.search_one("ＦＯＯ", ("prefix foo suffix",)), 1.0)
        self.assertEqual(backend.search_one("RAG", ("RAG数据索引",)), 1.0)
        self.assertIsNone(backend.search_one("RAG", ("encouraged",)))
        self.assertEqual(backend.search_one("数据", ("RAG数据索引",)), 1.0)
        self.assertIsNone(backend.search_one("bar", ("prefix foo suffix",)))


class AlternativeWeightTest(unittest.TestCase):
    def corpus(self, ubiquitous: int, rare: int, total: int) -> list[tuple[str, ...]]:
        return [
            (
                ("老师 " if index < ubiquitous else "")
                + ("日程" if index < rare else "别的"),
            )
            for index in range(total)
        ]

    def test_frequency_counts_documents_not_occurrences(self) -> None:
        corpus = [("老师 老师 老师",), ("老师",), ("别的",)]

        self.assertEqual(
            document_frequency(("老师", "别的"), corpus, StringSearchBackend()),
            {"老师": 2, "别的": 1},
        )

    def test_alternative_matching_most_of_the_corpus_loses_its_weight(self) -> None:
        weights = alternative_weights({"老师": 59, "日程": 1, "行程": 0}, 79)

        self.assertEqual(weights["老师"], 0.0)
        self.assertEqual(weights["日程"], 1.0)
        self.assertEqual(weights["行程"], 0.0)

    def test_small_corpus_keeps_every_alternative_at_full_weight(self) -> None:
        self.assertEqual(
            alternative_weights({"老师": 5, "日程": 1}, 5),
            {"老师": 1.0, "日程": 1.0},
        )

    def test_expression_ignores_alternatives_that_narrow_nothing(self) -> None:
        backend = StringSearchBackend()
        corpus = self.corpus(ubiquitous=59, rare=1, total=79)
        weights = alternative_weights(
            document_frequency(("老师", "日程", "行程"), corpus, backend), len(corpus)
        )

        self.assertIsNone(
            search_expression("老师|日程|行程", corpus[30], backend, weights=weights)
        )
        match = search_expression(
            "老师|日程|行程", corpus[0], backend, weights=weights
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.alternatives, ("日程",))
        self.assertEqual(match.score, 1.0)

    def test_expression_without_weights_keeps_plain_coverage_scoring(self) -> None:
        backend = StringSearchBackend()
        corpus = self.corpus(ubiquitous=59, rare=1, total=79)

        match = search_expression("老师|日程|行程", corpus[30], backend)

        self.assertIsNotNone(match)
        self.assertEqual(match.alternatives, ("老师",))
        self.assertEqual(match.score, 1 / 3)


if __name__ == "__main__":
    unittest.main()
