"""Batched query encoding and vector retrieval; model transport is injected."""
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import time
from typing import Protocol

import numpy as np

from .models import DenseMemoryHit, DenseThresholds, MemoryRecallQuery
from .snapshot import SegmentedVectorSnapshot, VectorMetadata


class QueryEncoder(Protocol):
    async def encode(self, texts: list[str], *, query: bool) -> list[list[float]]: ...


@dataclass(frozen=True)
class DenseSearchPool:
    document_types: set[str]
    after: float | None = None
    before: float | None = None
    group_by_parent: bool = False
    source_ids: frozenset[str] | None = None
    expressions: frozenset[str] | None = None


@dataclass(frozen=True)
class DenseSearchResult:
    space_id: str = ""
    hits: dict[str, list[tuple[VectorMetadata, float]]] = field(default_factory=dict)
    query_batch_size: int = 0
    request_ms: float = 0.0
    search_ms: float = 0.0
    fallback_reason: str = ""

    @property
    def memory(self) -> dict[str, dict[tuple[str, str], DenseMemoryHit]]:
        result = {}
        for expression, hits in self.hits.items():
            memory = {}
            for meta, cosine in hits:
                if meta.document_type != "confirmed_memory":
                    continue
                key = (meta.document_type, meta.source_id)
                previous = memory.get(key)
                if previous is None or cosine > previous.cosine:
                    memory[key] = DenseMemoryHit(meta.source_id, cosine)
            result[expression] = memory
        return result


class DenseQueryService:
    def __init__(
        self, snapshot: SegmentedVectorSnapshot, encoder: QueryEncoder | None, *,
        instruction: str = "", candidate_floor: int = 32, candidate_multiplier: int = 8,
        unavailable_reason: str = "",
        on_failure: Callable[[Exception, int], None] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.encoder = encoder
        self.instruction = instruction
        self.candidate_floor = candidate_floor
        self.candidate_multiplier = candidate_multiplier
        self.unavailable_reason = unavailable_reason
        self.on_failure = on_failure

    async def search(
        self, expressions: Iterable[str], pools: list[DenseSearchPool], *, limit: int = 8,
    ) -> DenseSearchResult:
        expressions = list(dict.fromkeys(value for value in expressions if value))
        space_id = self.snapshot.space_id
        if not expressions:
            return DenseSearchResult(space_id=space_id)
        if self.unavailable_reason or not space_id or self.encoder is None:
            return DenseSearchResult(
                space_id=space_id,
                fallback_reason=self.unavailable_reason or (
                    "no_active_space" if not space_id else "disabled"
                ),
            )
        started = time.monotonic()
        try:
            vectors = await self.encoder.encode(
                [self.instruction + value for value in expressions], query=True,
            )
            matrix = np.asarray(vectors, dtype=np.float32)
            if matrix.shape != (len(expressions), self.snapshot.dimensions):
                raise ValueError("query embedding shape mismatch")
            if not np.all(np.isfinite(matrix)):
                raise ValueError("query embedding contains non-finite values")
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            if not np.all(np.isfinite(norms)) or np.any(norms <= 0):
                raise ValueError("query embedding has zero or invalid norm")
            matrix = matrix / norms
        except Exception as error:
            if self.on_failure is not None:
                self.on_failure(error, len(expressions))
            return DenseSearchResult(
                space_id=space_id, query_batch_size=len(expressions),
                request_ms=(time.monotonic() - started) * 1000,
                fallback_reason=f"{type(error).__name__}: {str(error)[:160]}",
            )
        request_ms = (time.monotonic() - started) * 1000
        width = max(self.candidate_floor, max(1, limit) * self.candidate_multiplier)
        started = time.monotonic()
        hits: dict[str, list[tuple[VectorMetadata, float]]] = {
            expression: [] for expression in expressions
        }
        for pool in pools:
            indices = [i for i, expression in enumerate(expressions)
                       if pool.expressions is None or expression in pool.expressions]
            if not indices:
                continue
            for index, matches in self.snapshot.search(
                matrix[indices], pool.document_types, width, after=pool.after, before=pool.before,
                group_by_parent=pool.group_by_parent, source_ids=pool.source_ids,
            ).items():
                hits[expressions[indices[index]]].extend(matches)
        return DenseSearchResult(
            space_id=self.snapshot.space_id, hits=hits, query_batch_size=len(expressions),
            request_ms=request_ms, search_ms=(time.monotonic() - started) * 1000,
        )


@dataclass(frozen=True)
class VectorMemoryEvidence:
    memory: dict[str, dict[tuple[str, str], DenseMemoryHit]]
    calibration: Mapping[str, DenseThresholds]

    def thresholds(self, document_type: str) -> DenseThresholds | None:
        return self.calibration.get(document_type)


class MemoryVectorRecall:
    def __init__(
        self, queries: DenseQueryService, calibration: Mapping[str, DenseThresholds],
    ) -> None:
        self.queries = queries
        self.calibration = dict(calibration)

    @staticmethod
    def pools(eligible_ids: Mapping[str, frozenset[str]] | None):
        if eligible_ids is None:
            return [DenseSearchPool({"confirmed_memory"})]
        return [DenseSearchPool({"confirmed_memory"}, source_ids=ids,
                                expressions=frozenset({expression}))
                for expression, ids in eligible_ids.items()]

    async def __call__(
        self, queries: list[MemoryRecallQuery], limit: int, *,
        eligible_ids: Mapping[str, frozenset[str]] | None = None,
    ) -> VectorMemoryEvidence:
        result = await self.queries.search(
            (query.dense_expression for query in queries),
            self.pools(eligible_ids), limit=limit,
        )
        return VectorMemoryEvidence(result.memory, self.calibration)
