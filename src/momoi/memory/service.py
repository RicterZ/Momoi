"""Public memory API over a caller-owned database and injected model capabilities."""
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence

from .retrieval.bm25 import bm25_scores
from .retrieval.triggers import triggered_memories
from .metadata import MemoryFilters, TagCatalog, validate_scope
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
        self.writing = MemoryWritingService(database, self.repository, planner, recall=self.recall)

    async def plan(self, requests, *, evidence, snapshots=None, evidence_times=None, planner=None) -> MemoryPlan:
        return await self.writing.plan(requests, evidence=evidence, snapshots=snapshots, evidence_times=evidence_times, planner=planner)

    def apply(self, plan: MemoryPlan, *, operation_id: str) -> dict[str, object]:
        return self.writing.apply(plan, operation_id=operation_id)

    def triggered(self, text: str | Sequence[str], *, limit: int = 6, token_budget: int = 1600, scope: str = ""):
        """Bounded literal triggers over active global recall or one scope's memories; no model call."""
        return triggered_memories(self.repository, text, limit=limit, token_budget=token_budget, scope=scope)

    def bm25_scoped(self, text: str, *, scope: str, kinds: Sequence[str]):
        """BM25 of a text against every live memory of one scope; IDF comes from that scope only."""
        rows = self.repository.recall_rows(now=time.time(), filters={"scope": validate_scope(scope), "kinds": list(kinds)})
        if not rows or not text.strip():
            return []
        scores = bm25_scores(text, [f"{row['key']}\n{row['content']}" for row in rows])
        return [{**row, "bm25": score} for row, score in zip(rows, scores)]

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
