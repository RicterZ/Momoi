"""Public memory API over a caller-owned database and injected model capabilities."""
import sqlite3
from collections.abc import Callable, Mapping, Sequence

from .metadata import MemoryFilters, TagCatalog
from .writing.models import MemoryPlan, MemoryPlanner
from .writing.service import MemoryWritingService
from .indexing.source import MemoryIndexSource
from .retrieval.models import MemoryDenseEvidence, MemoryDenseRecall, MemoryRecallQuery
from .retrieval.rerank import MemoryReranker
from .retrieval.service import MemoryRecallService
from .retrieval.sparse import SearchBackend
from .storage.repository import MemoryRepository


class Memory:
    """Use an existing sqlite3.Row connection; the caller owns schema and lifetime."""

    def __init__(
        self, database: sqlite3.Connection, *, search_backend: SearchBackend | None = None,
        tags: TagCatalog | None = None, planner: MemoryPlanner | None = None,
        dense_recall: MemoryDenseRecall | None = None, reranker: MemoryReranker | None = None,
        reflection_rows: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    ) -> None:
        self.repository = MemoryRepository(database, tags=tags)
        self.recall = MemoryRecallService(
            self.repository, search_backend, dense_recall=dense_recall,
            reranker=reranker, reflection_rows=reflection_rows,
        )
        self.index_source = MemoryIndexSource(self.repository)
        self.writing = MemoryWritingService(database, self.repository, planner)

    async def plan(self, requests, *, evidence, snapshots=None, planner=None) -> MemoryPlan:
        return await self.writing.plan(requests, evidence=evidence, snapshots=snapshots, planner=planner)

    def apply(self, plan: MemoryPlan, *, operation_id: str) -> dict[str, object]:
        return self.writing.apply(plan, operation_id=operation_id)

    async def search(
        self, query: str | list[MemoryRecallQuery], limit: int = 6, *,
        filters: MemoryFilters | None = None,
        request: str | None = None, dense_evidence: MemoryDenseEvidence | None = None,
        reranker: MemoryReranker | None = None,
    ) -> list[dict[str, object]]:
        return await self.recall.search(
            query, limit, filters=filters, request=request, dense_evidence=dense_evidence, reranker=reranker,
        )

    def rank(
        self, queries: list[MemoryRecallQuery], limit: int, *, now: float | None = None,
        include_reflections: bool = False, dense_evidence: MemoryDenseEvidence | None = None,
        filters: MemoryFilters | None = None,
    ) -> list[dict[str, object]]:
        return self.recall.rank(
            queries, limit, now=now, include_reflections=include_reflections,
            dense_evidence=dense_evidence, filters=filters,
        )

    def search_literal(
        self, query: str, limit: int, *, include_core: bool = False,
        activation: str | None = None, include_scoped: bool = False,
        filters: MemoryFilters | None = None,
    ) -> list[dict[str, object]]:
        return self.recall.search_literal(
            query, limit, include_core=include_core,
            activation=activation, include_scoped=include_scoped, filters=filters,
        )

    def snapshots(self, ids: list[int]) -> dict[int, dict[str, object]]:
        return self.repository.snapshots(ids)
