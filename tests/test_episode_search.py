import unittest

from momoi.search import StringSearchBackend
from momoi.storage.episode_ranking import EpisodeRecallQuery, rank_episode_matches
from momoi.storage.episode_search import (
    EpisodeQueryService,
    EpisodeSearchDocument,
    EpisodeSearchField,
    EpisodeSearchHit,
    EpisodeSearchMessage,
    StringEpisodeSearchBackend,
)


def document(
    episode_id: str,
    *,
    title: str = "",
    topic: str = "",
    message: str = "",
    role: str = "assistant",
    delivery: str = "internal",
    last_activity: float = 0,
) -> EpisodeSearchDocument:
    messages = (
        EpisodeSearchMessage(
            id=hash(episode_id) % 100000,
            turn_id=f"turn-{episode_id}",
            ordinal=1,
            relation="primary",
            role=role,
            content=message,
            created_at=last_activity,
            delivery_state=delivery,
            timestamp="",
            searchable_text=message,
        ),
    ) if message else ()
    fields = [EpisodeSearchField("title", title)]
    if topic:
        fields.append(EpisodeSearchField("topic", topic))
    return EpisodeSearchDocument(
        episode_id=episode_id,
        fields=tuple(fields),
        last_activity_at=last_activity,
        salience=0.5,
        messages=messages,
    )


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def match_one(
        self,
        alternative: str,
        _: list[EpisodeSearchDocument],
    ) -> list[EpisodeSearchHit]:
        self.calls.append(alternative)
        return []


class EpisodeSearchTest(unittest.TestCase):
    def test_query_service_deduplicates_alternatives_without_ranking(self) -> None:
        backend = RecordingBackend()
        service = EpisodeQueryService(backend)

        results = service.match_many(
            ["房间|屋子", "屋子|卧室"],
            [],
        )

        self.assertEqual(backend.calls, ["房间", "屋子", "卧室"])
        self.assertEqual(
            [
                [alternative.alternative for alternative in result.alternatives]
                for result in results
            ],
            [["房间", "屋子"], ["屋子", "卧室"]],
        )

    def test_global_ranker_rewards_cross_query_coverage(self) -> None:
        documents = [
            document("shared", title="房间收纳", topic="蓝色杯子"),
            document("room-only", title="房间布置"),
            document("cup-only", topic="蓝色杯子"),
        ]
        queries = [
            EpisodeRecallQuery("房间", ("u1",), 0),
            EpisodeRecallQuery("蓝色杯子", ("u1",), 1),
        ]
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )

        ranked = rank_episode_matches(
            queries,
            service.match_many([query.expression for query in queries], documents),
            documents,
            limit=3,
            now=100,
        )

        self.assertEqual(ranked[0].episode_id, "shared")
        self.assertEqual(len(ranked[0].matched_queries), 2)

    def test_title_match_beats_incidental_internal_message(self) -> None:
        documents = [
            document("title", title="紫罗兰钥匙", last_activity=10),
            document(
                "internal",
                message="顺便提到紫罗兰钥匙",
                delivery="internal",
                last_activity=99,
            ),
        ]
        query = EpisodeRecallQuery("紫罗兰钥匙")
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )

        ranked = rank_episode_matches(
            [query],
            service.match_many([query.expression], documents),
            documents,
            limit=2,
            now=100,
        )

        self.assertEqual([item.episode_id for item in ranked], ["title", "internal"])

    def test_ranked_query_priority_is_preserved(self) -> None:
        documents = [
            document("primary", title="主查询"),
            document("tertiary", title="第三查询"),
        ]
        queries = [
            EpisodeRecallQuery("主查询", ("u1",), 0),
            EpisodeRecallQuery("第三查询", ("u1",), 2),
        ]
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )

        ranked = rank_episode_matches(
            queries,
            service.match_many([query.expression for query in queries], documents),
            documents,
            limit=2,
            now=100,
        )

        self.assertEqual([item.episode_id for item in ranked], ["primary", "tertiary"])

    def test_exact_internal_work_record_can_be_corroborated_by_next_query(self) -> None:
        documents = [
            EpisodeSearchDocument(
                episode_id="work-record",
                fields=(EpisodeSearchField("narrative_summary", "地平线6认景功课"),),
                last_activity_at=99,
                salience=0.5,
                messages=(
                    EpisodeSearchMessage(
                        id=1,
                        turn_id="turn-work",
                        ordinal=1,
                        relation="primary",
                        role="assistant",
                        content="已更新 fh6-japan-guide-notes",
                        created_at=99,
                        delivery_state="internal",
                        timestamp="",
                        searchable_text="已更新 fh6-japan-guide-notes",
                    ),
                ),
            ),
            document("topic-only", title="地平线6", last_activity=99),
        ]
        queries = [
            EpisodeRecallQuery("fh6-japan-guide-notes", ("u1",), 0),
            EpisodeRecallQuery("地平线6", ("u1",), 1),
        ]
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )

        ranked = rank_episode_matches(
            queries,
            service.match_many([query.expression for query in queries], documents),
            documents,
            limit=2,
            now=100,
        )

        self.assertEqual(
            [item.episode_id for item in ranked],
            ["work-record", "topic-only"],
        )

    def test_recency_anneals_semantic_score_without_erasing_old_evidence(self) -> None:
        now = 20 * 365 * 86400
        documents = [
            document("recent", title="线索", last_activity=now),
            document("half-life", title="线索", last_activity=now - 180 * 86400),
            document("ancient", title="线索", last_activity=0),
        ]
        query = EpisodeRecallQuery("线索")
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )

        ranked = rank_episode_matches(
            [query],
            service.match_many([query.expression], documents),
            documents,
            limit=3,
            now=now,
        )

        self.assertEqual(
            [item.episode_id for item in ranked],
            ["recent", "half-life", "ancient"],
        )
        factors = [
            (item.score - 0.05 * item.salience) / item.semantic_score
            for item in ranked
        ]
        self.assertAlmostEqual(factors[0], 1.0)
        self.assertAlmostEqual(factors[1], 0.9)
        self.assertAlmostEqual(factors[2], 0.8, places=6)

    def test_query_context_disambiguates_lexically_matching_episode(self) -> None:
        documents = [
            document("broad", title="双人合作"),
            document(
                "aligned",
                message="老师想找新的小游戏推荐，双人合作也可以",
                role="user",
                delivery="delivered",
            ),
        ]
        query = EpisodeRecallQuery(
            "双人合作",
            ("u1",),
            0,
            context="老师想找新的小游戏推荐",
        )
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )

        ranked = rank_episode_matches(
            [query],
            service.match_many([query.expression], documents),
            documents,
            limit=2,
            now=100,
        )

        self.assertEqual(
            [item.episode_id for item in ranked],
            ["aligned"],
        )
        self.assertGreater(ranked[0].context_score, 0)

    def test_ranker_defaults_to_six_and_filters_before_paging(self) -> None:
        documents = [
            document(f"strong-{index}", title="紫罗兰钥匙")
            for index in range(7)
        ] + [document("weak", message="钥匙")]
        query = EpisodeRecallQuery("钥匙|紫罗兰钥匙", context="紫罗兰钥匙")
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )
        matches = service.match_many([query.expression], documents)

        first = rank_episode_matches([query], matches, documents, now=100)
        second = rank_episode_matches(
            [query], matches, documents, limit=6, offset=6, now=100
        )

        self.assertEqual(len(first), 6)
        self.assertEqual(len(second), 1)
        self.assertNotIn("weak", {item.episode_id for item in first + second})
