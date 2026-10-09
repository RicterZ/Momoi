import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from momoi.memory.retrieval.models import DenseMemoryHit, DenseThresholds, MemoryRecallQuery
from momoi.memory.retrieval.service import MemoryRecallService
from momoi.memory.storage.repository import MemoryRepository
from momoi.memory.retrieval.rerank import SelectionProtocolError
from tests.test_memory_repository import database, write


def dense(query, *ids):
    return SimpleNamespace(
        memory={query: {("confirmed_memory", str(i)): DenseMemoryHit(str(i), 0.95)
                        for i in ids}},
        thresholds=lambda _: DenseThresholds(0.5, 0.7, 0.9),
    )


def test_natural_query_uses_injected_dense_recall_then_one_rerank(database):
    repo = MemoryRepository(database)
    coffee = write(repo)
    tea = write(repo, key="tea", text="也喜欢无糖茶")
    unrelated = write(repo, key="unrelated", text="喜欢骑自行车")
    query = "用户喜欢什么饮品"
    embedding = AsyncMock(return_value=dense(query, coffee, tea))
    seen = []

    async def rerank(request, candidates):
        assert request == query
        seen.extend(candidates)
        return list(reversed(candidates))

    reranker = AsyncMock(side_effect=rerank)
    service = MemoryRecallService(repo, dense_recall=embedding, reranker=reranker)
    results = asyncio.run(service.search(query))
    assert {row["id"] for row in results} == {coffee, tea}
    assert results == list(reversed(seen))
    assert all(row["dense_only"] for row in results)
    embedding.assert_awaited_once_with(
        [MemoryRecallQuery(query)], 24,
        eligible_ids={query: frozenset(str(i) for i in (coffee, tea, unrelated))},
    )
    reranker.assert_awaited_once()


def test_prepared_evidence_is_reused_and_empty_memory_pool_still_calls_joint_adapter(database):
    repo = MemoryRepository(database)
    coffee = write(repo)
    query = "用户喜欢什么饮品"
    embedding = AsyncMock(side_effect=AssertionError("must reuse prepared vectors"))
    reranker = AsyncMock(side_effect=lambda request, rows: rows)
    service = MemoryRecallService(repo, dense_recall=embedding)
    results = asyncio.run(service.search(
        query, dense_evidence=dense(query, coffee), reranker=reranker,
    ))
    assert [row["id"] for row in results] == [coffee]
    assert asyncio.run(service.search(query, limit=0, reranker=reranker)) == []
    embedding.assert_not_awaited()
    assert reranker.await_count == 2
    assert reranker.await_args.args == (query, [])


@pytest.mark.parametrize("bad_result", ["duplicate", "rewrite", "unknown", "nonlist"])
def test_reranker_cannot_modify_or_invent_candidates(database, bad_result):
    repo = MemoryRepository(database)
    write(repo)

    async def rerank(request, candidates):
        if bad_result == "duplicate":
            return candidates * 2
        if bad_result == "rewrite":
            candidates[0]["content"] = "伪造的偏好"
            return candidates
        if bad_result == "unknown":
            candidates[0]["id"] = 999
            return candidates
        return None

    service = MemoryRecallService(repo, reranker=rerank)
    with pytest.raises(SelectionProtocolError):
        asyncio.run(service.search("无糖咖啡"))
    assert repo.active("preference", "drink")["content"] == "喜欢无糖咖啡"


def test_unconfirmed_rows_are_opt_in_and_dense_hits_cannot_restore_deleted_memory(database):
    repo = MemoryRepository(database)
    identifier = write(repo)
    reflections = lambda: [{"id": 1, "kind": "preference", "key": "drink",
                           "content": "喜欢无糖咖啡", "confidence": 0.9,
                           "evidence": "观察", "updated_at": 1, "local_date": "2026-01-01"}]
    service = MemoryRecallService(repo, reflection_rows=reflections)
    queries = [MemoryRecallQuery("无糖咖啡")]
    assert {row["source"] for row in service.rank(queries, 6)} == {"confirmed"}
    assert {row["source"] for row in service.rank(queries, 6, include_reflections=True)} == {
        "confirmed", "reflection",
    }
    repo.forget(repo.snapshots([identifier])[identifier],
                {"event_id": "forget", "quote": "忘记"}, now=2)
    assert asyncio.run(service.search(
        "用户喜欢什么饮品", dense_evidence=dense("用户喜欢什么饮品", identifier),
    )) == []
