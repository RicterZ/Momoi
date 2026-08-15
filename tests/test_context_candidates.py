import unittest

from momoi.runtime.context_candidates import (
    DEFAULT_EPISODE_CANDIDATE_POLICY,
    EpisodeCandidatePolicy,
    collect_episode_candidates,
    full_candidate_context,
)


class ContextCandidatesTest(unittest.TestCase):
    def test_default_policy_uses_evaluated_limits(self) -> None:
        self.assertEqual(
            DEFAULT_EPISODE_CANDIDATE_POLICY,
            EpisodeCandidatePolicy(8, 2, 8, 18),
        )

    def test_collects_in_priority_order_and_deduplicates(self) -> None:
        class Store:
            def search_episodes(self, _query: str, _limit: int, **_: object):
                return [{"id": "matched"}, {"id": "shared"}]

            def list_episode_candidates(self, _limit: int, **_: object):
                return [{"id": "shared"}, {"id": "active"}]

            def list_episode_directory(self, _limit: int, **_: object):
                return [{"id": "directory"}, {"id": "older"}]

        result = collect_episode_candidates(
            Store(),  # type: ignore[arg-type]
            "query",
            EpisodeCandidatePolicy(2, 2, 2, 4),
        )
        self.assertEqual(
            [item["id"] for item in result],
            ["matched", "shared", "active", "directory"],
        )

    def test_full_context_preserves_existing_shape_and_truncates_summary(self) -> None:
        result = full_candidate_context(
            [
                {
                    "id": "episode",
                    "status": "open",
                    "title": "Topic",
                    "created_timestamp": "created",
                    "updated_timestamp": "updated",
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
        self.assertEqual(result[0]["open_loops"], ["loop"])
