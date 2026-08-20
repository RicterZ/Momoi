import unittest

from momoi.runtime.context_candidates import (
    DEFAULT_EPISODE_CANDIDATE_POLICY,
    EpisodeCandidatePolicy,
    collect_episode_candidates,
    full_candidate_context,
    rank_episode_candidates,
)


class ContextCandidatesTest(unittest.TestCase):
    def test_default_policy_uses_evaluated_limits(self) -> None:
        self.assertEqual(
            DEFAULT_EPISODE_CANDIDATE_POLICY,
            EpisodeCandidatePolicy(32, 12, 64, 8),
        )

    def test_collects_in_priority_order_and_deduplicates(self) -> None:
        class Store:
            def episode_context_scores(self, _turn_ids: list[str]):
                return {}

            def search_episodes(self, _query: str, _limit: int, **_: object):
                return [
                    {"id": "matched", "title": "matched"},
                    {"id": "shared", "title": "shared"},
                ]

            def list_episode_candidates(self, _limit: int, **_: object):
                return [
                    {"id": "shared", "title": "shared"},
                    {"id": "active", "title": "active"},
                ]

            def list_episode_directory(self, _limit: int, **_: object):
                return [
                    {"id": "directory", "title": "directory"},
                    {"id": "older", "title": "older"},
                ]

            def episode_candidates_by_ids(self, _episode_ids: list[str]):
                return []

        result = collect_episode_candidates(
            Store(),  # type: ignore[arg-type]
            "query",
            EpisodeCandidatePolicy(2, 2, 2, 4),
        )
        ids = [item["id"] for item in result]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("shared", ids)

    def test_full_context_preserves_existing_shape_and_truncates_summary(self) -> None:
        result = full_candidate_context(
            [
                {
                    "id": "episode",
                    "status": "open",
                    "title": "Topic",
                    "created_timestamp": "created",
                    "last_activity_timestamp": "activity",
                    "working_summary": "x" * 500,
                    "summary": "",
                    "topics": ["topic"],
                    "entities": ["entity"],
                    "open_loops": ["loop"],
                }
            ]
        )
        self.assertEqual(len(result[0]["summary"]), 400)
        self.assertEqual(result[0]["id"], "episode")
        self.assertNotIn("last_activity_timestamp", result[0])
        self.assertNotIn("updated_timestamp", result[0])
        self.assertEqual(result[0]["open_loops"], ["loop"])

    def test_recent_context_beats_broad_title_match(self) -> None:
        ranked = rank_episode_candidates(
            [
                {
                    "id": "broad",
                    "status": "closed",
                    "title": "下班前后陪伴",
                    "topics": ["陪伴"],
                    "entities": [],
                    "open_loops": [],
                    "last_activity_at": 990,
                },
                {
                    "id": "recent",
                    "status": "open",
                    "title": "想独处一会儿",
                    "topics": ["独处", "边界"],
                    "entities": [],
                    "open_loops": ["等主人主动回来"],
                    "last_activity_at": 995,
                },
            ],
            "我准备下班了",
            {"recent": {"recent_context": 1.0}},
            2,
            now=1000,
        )

        self.assertEqual([item["id"] for item in ranked], ["recent", "broad"])

    def test_explicit_metadata_can_beat_unrelated_recent_context(self) -> None:
        ranked = rank_episode_candidates(
            [
                {
                    "id": "recent",
                    "status": "closing",
                    "title": "刚刚下班",
                    "topics": ["下班"],
                    "entities": [],
                    "open_loops": [],
                    "last_activity_at": 999,
                },
                {
                    "id": "overtime",
                    "status": "closed",
                    "title": "周末加班安排",
                    "topics": ["加班", "周日加班", "工作疲惫"],
                    "entities": ["公司"],
                    "open_loops": [],
                    "matches": [
                        {"role": "user", "content": "明天还得加班"}
                    ],
                    "last_activity_at": 900,
                },
            ],
            "明天还得加班 苦哈哈",
            {"recent": {"recent_context": 1.0}},
            2,
            now=1000,
        )

        self.assertEqual([item["id"] for item in ranked], ["overtime", "recent"])

    def test_episode_size_is_not_a_ranking_feature(self) -> None:
        base = {
            "status": "closed",
            "title": "提示词升级",
            "topics": ["提示词", "代码升级"],
            "entities": ["Momoi"],
            "open_loops": [],
            "last_activity_at": 900,
        }
        ranked = rank_episode_candidates(
            [
                {"id": "small", **base, "turn_count": 1},
                {"id": "large", **base, "turn_count": 500},
            ],
            "刚升级了提示词和代码",
            {},
            2,
            now=1000,
        )

        self.assertEqual(
            ranked[0]["match_features"], ranked[1]["match_features"]
        )
