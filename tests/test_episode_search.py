import math
import unittest

from momoi.search import StringSearchBackend
from momoi.storage.episode_ranking import (
    EpisodeRecallQuery,
    _saturate_sparse_score,
    rank_episode_matches,
)
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
    def test_sparse_score_saturation_is_monotonic_and_bounded(self) -> None:
        raw_scores = (0.0, 0.5, 2.0, 8.0, 100.0)
        saturated = tuple(_saturate_sparse_score(score) for score in raw_scores)

        self.assertEqual(saturated[0], 0.0)
        self.assertTrue(
            all(left < right for left, right in zip(saturated, saturated[1:]))
        )
        self.assertAlmostEqual(saturated[2], 2.0 * (1.0 - math.exp(-1.0)))
        self.assertTrue(all(score <= 2.0 for score in saturated[1:]))
        self.assertAlmostEqual(saturated[-1], 2.0)

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

    def test_independent_query_evidence_is_not_vetoed_by_other_queries(self) -> None:
        documents = [
            document(
                "current-banter",
                title="红色礼盒玩笑",
                message="签收礼物，交换卡片，确认约定",
                role="user",
                delivery="delivered",
            ),
            EpisodeSearchDocument(
                episode_id="agent-limit",
                fields=(
                    EpisodeSearchField("topic", "联机模式"),
                    EpisodeSearchField(
                        "working_summary",
                        "现在还不能进入联机模式，需要客户端能力升级",
                    ),
                ),
                last_activity_at=0,
                salience=0.5,
                messages=tuple(
                    EpisodeSearchMessage(
                        id=index,
                        turn_id=f"turn-{index}",
                        ordinal=index,
                        relation="primary",
                        role="user",
                        content="目前没法一起进入联机模式",
                        created_at=0,
                        delivery_state="delivered",
                        timestamp="",
                        searchable_text="目前没法一起进入联机模式",
                    )
                    for index in range(1, 5)
                ),
            ),
        ]
        queries = [
            EpisodeRecallQuery("红色礼盒", ("u1",), 0),
            EpisodeRecallQuery("联机模式", ("u1",), 1),
            EpisodeRecallQuery(
                "签收礼物|交换卡片|确认约定",
                ("u1",),
                2,
            ),
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

        hits = {item.episode_id: item for item in ranked}
        self.assertIn("agent-limit", hits)
        self.assertGreaterEqual(hits["agent-limit"].relevance_confidence, 0.47)

    def test_query_order_changes_rank_not_independent_eligibility(self) -> None:
        documents = [document("history", topic="联机模式")]
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )

        confidences = []
        scores = []
        for queries in (
            [
                EpisodeRecallQuery("联机模式", ("u1",), 0),
                EpisodeRecallQuery("红色礼盒", ("u1",), 1),
            ],
            [
                EpisodeRecallQuery("红色礼盒", ("u1",), 0),
                EpisodeRecallQuery("联机模式", ("u1",), 1),
            ],
        ):
            ranked = rank_episode_matches(
                queries,
                service.match_many(
                    [query.expression for query in queries], documents
                ),
                documents,
                limit=2,
                now=100,
            )
            self.assertEqual([item.episode_id for item in ranked], ["history"])
            confidences.append(ranked[0].relevance_confidence)
            scores.append(ranked[0].score)

        self.assertAlmostEqual(confidences[0], confidences[1])
        self.assertGreater(scores[0], scores[1])

    def test_parallel_aliases_are_or_for_eligibility_and_bonus_for_rank(self) -> None:
        documents = [
            document(
                "one-alias",
                title="服务异常",
                topic="服务异常",
                message="用户正在描述系统状态：服务异常",
                role="user",
                delivery="delivered",
            ),
            document(
                "two-aliases",
                title="连接超时和服务异常",
                topic="连接超时和服务异常",
                message="用户正在描述系统状态：连接超时和服务异常",
                role="user",
                delivery="delivered",
            ),
        ]
        query = EpisodeRecallQuery(
            "连接超时|服务异常",
            ("u1",),
            0,
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

        hits = {item.episode_id: item for item in ranked}
        self.assertEqual(set(hits), {"one-alias", "two-aliases"})
        self.assertGreaterEqual(hits["one-alias"].relevance_confidence, 0.47)
        self.assertGreaterEqual(hits["two-aliases"].relevance_confidence, 0.47)
        self.assertGreater(
            hits["two-aliases"].score,
            hits["one-alias"].score,
        )

    def test_incidental_internal_match_without_field_support_stays_filtered(
        self,
    ) -> None:
        documents = [
            document(
                "incidental",
                message="日志末尾顺便出现紫罗兰钥匙",
                delivery="internal",
            )
        ]
        queries = [
            EpisodeRecallQuery(
                "紫罗兰钥匙",
                ("u1",),
                2,
            )
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

        self.assertEqual(ranked, [])

    def test_short_prose_literal_needs_selective_or_repeated_support(self) -> None:
        documents = [
            EpisodeSearchDocument(
                episode_id="summary-only",
                fields=(
                    EpisodeSearchField("narrative_summary", "原来的方案保持不变"),
                    EpisodeSearchField("working_summary", "整体方向不变"),
                ),
                last_activity_at=0,
                salience=0.5,
                messages=tuple(
                    EpisodeSearchMessage(
                        id=index,
                        turn_id=f"turn-{index}",
                        ordinal=index,
                        relation="primary",
                        role="user",
                        content="方案不变",
                        created_at=0,
                        delivery_state="delivered",
                        timestamp="",
                        searchable_text="方案不变",
                    )
                    for index in range(1, 3)
                ),
            ),
            document("topic", topic="暗号", message="暗号"),
        ]
        queries = [EpisodeRecallQuery("不变"), EpisodeRecallQuery("暗号")]
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

        self.assertEqual([item.episode_id for item in ranked], ["topic"])

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

        self.assertEqual([item.episode_id for item in ranked], ["title"])

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

    def test_unscoped_prose_does_not_override_structured_exact_evidence(self) -> None:
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
            ["broad"],
        )

    def test_ranker_defaults_to_eight_and_filters_before_paging(self) -> None:
        documents = [
            document(f"strong-{index}", title="紫罗兰钥匙")
            for index in range(9)
        ] + [document("weak", message="钥匙")]
        query = EpisodeRecallQuery("钥匙|紫罗兰钥匙")
        service = EpisodeQueryService(
            StringEpisodeSearchBackend(StringSearchBackend())
        )
        matches = service.match_many([query.expression], documents)

        first = rank_episode_matches([query], matches, documents, now=100)
        second = rank_episode_matches(
            [query], matches, documents, limit=8, offset=8, now=100
        )

        self.assertEqual(len(first), 8)
        self.assertEqual(len(second), 1)
        self.assertNotIn("weak", {item.episode_id for item in first + second})
