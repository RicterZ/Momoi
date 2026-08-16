import unittest

from momoi.storage.episode_search import (
    EpisodeQueryService,
    EpisodeSearchDocument,
    EpisodeSearchHit,
)


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search_one(
        self,
        keyword: str,
        documents: list[EpisodeSearchDocument],
        max_results: int,
    ) -> list[EpisodeSearchHit]:
        self.calls.append(keyword)
        results = {
            "房间": [
                EpisodeSearchHit("shared", 1.0, 30, ()),
                EpisodeSearchHit("room-only", 1.0, 20, ()),
            ],
            "屋子": [
                EpisodeSearchHit("shared", 1.0, 30, ()),
                EpisodeSearchHit("house-only", 1.0, 10, ()),
            ],
        }
        return results.get(keyword, [])[:max_results]


class EpisodeSearchTest(unittest.TestCase):
    def test_query_service_calls_backend_once_per_complete_alternative(self) -> None:
        backend = RecordingBackend()
        service = EpisodeQueryService(backend)

        results = service.search("房间 | 屋子 | 房间", [], 3)

        self.assertEqual(backend.calls, ["房间", "屋子"])
        self.assertEqual(
            [result.episode_id for result in results],
            ["shared", "room-only", "house-only"],
        )

    def test_query_service_pages_after_global_merge(self) -> None:
        backend = RecordingBackend()
        service = EpisodeQueryService(backend)

        first = service.search("房间 | 屋子", [], 2)
        second = service.search("房间 | 屋子", [], 2, offset=2)

        self.assertEqual(
            [result.episode_id for result in first],
            ["shared", "room-only"],
        )
        self.assertEqual(
            [result.episode_id for result in second],
            ["house-only"],
        )
