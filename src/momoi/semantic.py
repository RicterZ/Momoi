from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import httpx
import numpy as np

from .config import EmbeddingConfig
from .logging_context import log_event
from .storage import MemoryRecallQuery, Store, decode_vector
from .storage.episode_ranking import EpisodeRecallQuery

logger = logging.getLogger(__name__)


def semantic_error_category(error: BaseException) -> str:
    """Classify embedding failures without changing the fallback payload."""
    return "timeout" if isinstance(error, TimeoutError) else "error"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
CALIBRATION_PROFILES: dict[str, dict[str, tuple[float, float, float]]] = {
    # Calibrated against the private historical benchmark for this model. The
    # episode-only gate is intentionally stricter: generic episode summaries
    # otherwise produce high cosine scores without sparse/topic corroboration.
    "bge-small-zh-v1.5-momoi-v1": {
        "confirmed_memory": (0.55, 0.72, 0.86),
        "reflection_memory": (0.58, 0.75, 0.87),
        "episode_summary": (0.52, 0.81, 0.84),
        "episode_turn": (0.56, 0.81, 0.86),
    }
}


@dataclass(frozen=True)
class DenseThresholds:
    support: float
    only: float
    strong: float

    def calibrated(self, cosine: float) -> float:
        if self.strong <= self.support:
            return 0.0
        return min(
            1.0, max(0.0, (cosine - self.support) / (self.strong - self.support))
        )


@dataclass(frozen=True)
class DenseMemoryHit:
    source_id: str
    cosine: float


@dataclass(frozen=True)
class DenseEpisodeHit:
    episode_id: str
    summary_cosine: float | None = None
    turn_cosine: float | None = None

    @property
    def cosine(self) -> float:
        return max(
            value
            for value in (self.summary_cosine, self.turn_cosine)
            if value is not None
        )


@dataclass(frozen=True)
class DenseRecallEvidence:
    space_id: str = ""
    calibration_profile: str = ""
    memory: dict[str, dict[tuple[str, str], DenseMemoryHit]] = field(
        default_factory=dict
    )
    episodes: dict[str, dict[str, DenseEpisodeHit]] = field(default_factory=dict)
    query_batch_size: int = 0
    request_ms: float = 0.0
    search_ms: float = 0.0
    fallback_reason: str = ""

    def thresholds(self, document_type: str) -> DenseThresholds | None:
        values = CALIBRATION_PROFILES.get(self.calibration_profile, {}).get(
            document_type
        )
        return DenseThresholds(*values) if values else None


@dataclass(frozen=True)
class _VectorMeta:
    key: tuple[str, str, int]
    document_type: str
    source_id: str
    parent_id: str
    starts_at: float | None
    ends_at: float | None
    generation: int


@dataclass(frozen=True)
class _VectorSegment:
    vectors: np.ndarray
    metadata: tuple[_VectorMeta, ...]


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient()
        self._query_failures = 0
        self._query_breaker_until = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def encode(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        now = time.monotonic()
        if query and now < self._query_breaker_until:
            raise RuntimeError("query circuit breaker is open")
        headers = (
            {"Authorization": f"Bearer {self.config.api_key}"}
            if self.config.api_key
            else {}
        )
        timeout = (
            self.config.query_timeout_seconds
            if query
            else self.config.document_timeout_seconds
        )
        try:
            response = await self._client.post(
                self.config.endpoint,
                json={"model": self.config.model, "input": texts},
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("embedding response count mismatch")
            ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
            vectors = [item.get("embedding") for item in ordered]
            if any(not isinstance(vector, list) for vector in vectors):
                raise ValueError("embedding response has invalid vectors")
            normalized: list[list[float]] = []
            for raw in vectors:
                array = np.asarray(raw, dtype=np.float32)
                if array.ndim != 1 or array.size != self.config.dimensions:
                    raise ValueError("embedding dimension mismatch")
                if not np.isfinite(array).all():
                    raise ValueError("embedding contains non-finite values")
                norm = float(np.linalg.norm(array))
                if not math.isfinite(norm) or norm <= 0:
                    raise ValueError("embedding has invalid norm")
                normalized.append((array / norm).tolist())
            if query:
                self._query_failures = 0
            return normalized
        except Exception:
            if query:
                self._query_failures += 1
                if self._query_failures >= 2:
                    self._query_breaker_until = time.monotonic() + 30.0
            raise

    async def health(self) -> tuple[bool, float, str]:
        started = time.monotonic()
        try:
            url = self.config.endpoint.rsplit("/v1/embeddings", 1)[0] + "/healthz"
            response = await self._client.get(
                url, timeout=self.config.query_timeout_seconds
            )
            response.raise_for_status()
            return True, (time.monotonic() - started) * 1000, ""
        except Exception as error:
            return False, (time.monotonic() - started) * 1000, type(error).__name__


class SegmentedVectorSnapshot:
    def __init__(self, store: Store, dimensions: int) -> None:
        self.store = store
        self.dimensions = dimensions
        self.space_id = ""
        self._segments: list[_VectorSegment] = []
        self._latest: dict[tuple[str, str, int], int] = {}
        self._generation = 0

    def load(self, space_id: str) -> None:
        self.space_id = space_id
        self._segments = []
        self._latest = {}
        self._generation = 0
        for rows in self.store.semantic_ready_documents(space_id):
            self._append(rows)

    def _append(self, rows: list[dict[str, object]]) -> None:
        vectors: list[np.ndarray] = []
        metadata: list[_VectorMeta] = []
        for row in rows:
            key = (
                str(row["document_type"]),
                str(row["source_id"]),
                int(row["chunk_index"]),
            )
            try:
                if int(row["dimensions"] or 0) != self.dimensions:
                    raise ValueError("stored embedding dimension mismatch")
                vector = decode_vector(row["vector"], self.dimensions)
            except ValueError as error:
                self.store.invalidate_semantic_document(self.space_id, *key, str(error))
                continue
            self._generation += 1
            generation = self._generation
            self._latest[key] = generation
            vectors.append(vector)
            metadata.append(
                _VectorMeta(
                    key,
                    key[0],
                    key[1],
                    str(row["parent_id"] or ""),
                    float(row["starts_at"]) if row["starts_at"] is not None else None,
                    float(row["ends_at"]) if row["ends_at"] is not None else None,
                    generation,
                )
            )
        if vectors:
            self._segments.append(
                _VectorSegment(np.ascontiguousarray(np.stack(vectors)), tuple(metadata))
            )

    def replace_source(self, source_type: str, source_id: str) -> None:
        stale = [
            key
            for key in self._latest
            if (
                key[0] in {"episode_summary", "episode_turn"}
                and source_type == "episode"
                and any(
                    meta.key == key and meta.parent_id == source_id
                    for segment in self._segments
                    for meta in segment.metadata
                )
            )
            or (key[0] == source_type and key[1] == source_id)
            or (
                key[0] == "episode_summary"
                and source_type == "episode"
                and key[1] == source_id
            )
        ]
        for key in stale:
            self._latest.pop(key, None)
        rows = self.store.semantic_ready_source_documents(
            self.space_id, source_type, source_id
        )
        self._append(rows)
        if len(self._segments) > 64:
            self.load(self.space_id)

    def search(
        self,
        query_vectors: np.ndarray,
        document_types: set[str],
        limit: int,
        *,
        after: float | None = None,
        before: float | None = None,
    ) -> dict[int, list[tuple[_VectorMeta, float]]]:
        candidates: dict[int, list[tuple[_VectorMeta, float]]] = {
            index: [] for index in range(len(query_vectors))
        }
        width = max(1, limit)
        for segment in self._segments:
            scores = query_vectors @ segment.vectors.T
            for query_index in range(scores.shape[0]):
                row_scores = scores[query_index]
                if document_types.issubset({"episode_summary", "episode_turn"}):
                    indices = range(len(row_scores))
                else:
                    take = min(width, len(row_scores))
                    indices = np.argpartition(row_scores, -take)[-take:]
                for index in indices:
                    meta = segment.metadata[int(index)]
                    if meta.document_type not in document_types:
                        continue
                    if self._latest.get(meta.key) != meta.generation:
                        continue
                    if after is not None and (
                        meta.ends_at is None or meta.ends_at < after
                    ):
                        continue
                    if before is not None and (
                        meta.starts_at is None or meta.starts_at >= before
                    ):
                        continue
                    candidates[query_index].append((meta, float(row_scores[index])))
        for query_index, hits in candidates.items():
            hits.sort(key=lambda item: item[1], reverse=True)
            if document_types.issubset({"episode_summary", "episode_turn"}):
                best: dict[tuple[str, str], tuple[_VectorMeta, float]] = {}
                for meta, score in hits:
                    key = (meta.parent_id or meta.source_id, meta.document_type)
                    if key not in best:
                        best[key] = (meta, score)
                hits = sorted(best.values(), key=lambda item: item[1], reverse=True)
            candidates[query_index] = hits[:width]
        return candidates


class SemanticRecallService:
    def __init__(
        self, store: Store, config: EmbeddingConfig, *, auto_activate: bool = True
    ) -> None:
        self.store = store
        self.config = config
        self.client = EmbeddingClient(config)
        self.snapshot = SegmentedVectorSnapshot(store, config.dimensions)
        self.degraded_reason = "disabled" if not config.enabled else "no_active_space"
        self.auto_activate = auto_activate
        self._needs_reconciliation = False

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self.config.calibration_profile not in CALIBRATION_PROFILES:
            self.degraded_reason = "unknown_calibration_profile"
            return
        space = self.store.semantic_space(state="active")
        if space is None:
            self.store.ensure_semantic_space(
                model=self.config.model,
                dimensions=self.config.dimensions,
                calibration_profile=self.config.calibration_profile,
            )
            self._needs_reconciliation = True
            self.degraded_reason = "building_initial_space"
            return
        if (
            str(space["model"]) != self.config.model
            or int(space["dimensions"]) != self.config.dimensions
            or str(space["calibration_profile"]) != self.config.calibration_profile
        ):
            self.degraded_reason = "active_space_mismatch"
            self.store.ensure_semantic_space(
                model=self.config.model,
                dimensions=self.config.dimensions,
                calibration_profile=self.config.calibration_profile,
            )
            self._needs_reconciliation = True
            return
        self.snapshot.load(str(space["id"]))
        self._needs_reconciliation = True
        self.degraded_reason = ""

    async def close(self) -> None:
        await self.client.close()

    @staticmethod
    def _expressions(
        queries: Iterable[MemoryRecallQuery | EpisodeRecallQuery],
    ) -> list[str]:
        return list(
            dict.fromkeys(
                query.dense_expression
                for query in queries
                if query.dense_expression
            )
        )

    async def prepare(
        self,
        queries: Iterable[MemoryRecallQuery | EpisodeRecallQuery],
        *,
        include_memory: bool = True,
        include_episode: bool = True,
        episode_after: float | None = None,
        episode_before: float | None = None,
        output_limit: int = 8,
    ) -> DenseRecallEvidence:
        expressions = self._expressions(queries)
        if not expressions:
            return DenseRecallEvidence(
                space_id=self.snapshot.space_id,
                calibration_profile=self.config.calibration_profile,
            )
        if (
            not self.config.enabled
            or self.degraded_reason
            or not self.snapshot.space_id
        ):
            return DenseRecallEvidence(
                space_id=self.snapshot.space_id,
                calibration_profile=self.config.calibration_profile,
                fallback_reason=self.degraded_reason or "disabled",
            )
        request_started = time.monotonic()
        try:
            vectors = await self.client.encode(
                [QUERY_INSTRUCTION + expression for expression in expressions],
                query=True,
            )
        except Exception as error:
            error_type = type(error).__name__
            reason = f"{error_type}: {str(error)[:160]}"
            category = semantic_error_category(error)
            log_event(
                logger,
                logging.WARNING,
                "semantic_query_fallback",
                reason=reason,
                error_type=error_type,
                category=category,
                query_batch_size=len(expressions),
            )
            return DenseRecallEvidence(
                space_id=self.snapshot.space_id,
                calibration_profile=self.config.calibration_profile,
                query_batch_size=len(expressions),
                request_ms=(time.monotonic() - request_started) * 1000,
                fallback_reason=reason,
            )
        request_ms = (time.monotonic() - request_started) * 1000
        matrix = np.asarray(vectors, dtype=np.float32)
        candidate_width = max(32, max(1, output_limit) * 8)
        search_started = time.monotonic()
        memory_hits: dict[int, list[tuple[_VectorMeta, float]]] = {}
        if include_memory:
            for document_type in ("confirmed_memory", "reflection_memory"):
                per_pool = self.snapshot.search(
                    matrix, {document_type}, candidate_width
                )
                for index, hits in per_pool.items():
                    memory_hits.setdefault(index, []).extend(hits)
        episode_types = (
            {"episode_turn"}
            if episode_after is not None or episode_before is not None
            else {"episode_summary", "episode_turn"}
        )
        episode_hits: dict[int, list[tuple[_VectorMeta, float]]] = {}
        if include_episode:
            for document_type in episode_types:
                per_pool = self.snapshot.search(
                    matrix,
                    {document_type},
                    candidate_width,
                    after=episode_after,
                    before=episode_before,
                )
                for index, hits in per_pool.items():
                    episode_hits.setdefault(index, []).extend(hits)
        memory: dict[str, dict[tuple[str, str], DenseMemoryHit]] = {}
        episodes: dict[str, dict[str, DenseEpisodeHit]] = {}
        for index, expression in enumerate(expressions):
            expression_memory: dict[tuple[str, str], DenseMemoryHit] = {}
            episode_values: dict[str, dict[str, float]] = {}
            for meta, cosine in memory_hits.get(index, []):
                key = (meta.document_type, meta.source_id)
                previous = expression_memory.get(key)
                if previous is None or cosine > previous.cosine:
                    expression_memory[key] = DenseMemoryHit(meta.source_id, cosine)
            for meta, cosine in episode_hits.get(index, []):
                episode_id = meta.parent_id or meta.source_id
                field_name = (
                    "summary_cosine"
                    if meta.document_type == "episode_summary"
                    else "turn_cosine"
                )
                values = episode_values.setdefault(episode_id, {})
                values[field_name] = max(cosine, values.get(field_name, -1.0))
            memory[expression] = expression_memory
            episodes[expression] = {
                episode_id: DenseEpisodeHit(episode_id, **values)
                for episode_id, values in episode_values.items()
            }
        return DenseRecallEvidence(
            space_id=self.snapshot.space_id,
            calibration_profile=self.config.calibration_profile,
            memory=memory,
            episodes=episodes,
            query_batch_size=len(expressions),
            request_ms=request_ms,
            search_ms=(time.monotonic() - search_started) * 1000,
        )

    async def maintain_once(self, *, allow_encoding: bool = True) -> bool:
        worked = False
        claims = self.store.claim_semantic_sources(16)
        for claim in claims:
            try:
                changed = self.store.materialize_semantic_source(claim)
                worked = worked or bool(changed)
                if self.snapshot.space_id:
                    self.snapshot.replace_source(
                        str(claim["source_type"]), str(claim["source_id"])
                    )
            except Exception as error:
                self.store.fail_semantic_source(claim, error)
                log_event(
                    logger,
                    logging.ERROR,
                    "semantic_materialize_failed",
                    source_type=claim.get("source_type"),
                    error_type=type(error).__name__,
                )
        if not allow_encoding:
            return worked
        spaces = [
            space
            for state in ("building", "active")
            if (space := self.store.semantic_space(state=state)) is not None
        ]
        for space in spaces:
            rows = self.store.claim_semantic_documents(
                str(space["id"]), self.config.document_batch_size
            )
            if not rows:
                continue
            worked = True
            try:
                vectors = await self.client.encode(
                    [str(row["content"]) for row in rows], query=False
                )
                self.store.finish_semantic_documents(
                    rows, vectors, int(space["dimensions"])
                )
                if str(space["id"]) == self.snapshot.space_id:
                    for source_type, source_id in dict.fromkeys(
                        (
                            "episode"
                            if str(row["document_type"]).startswith("episode_")
                            else str(row["document_type"]),
                            str(row["parent_id"] or row["source_id"]),
                        )
                        for row in rows
                    ):
                        self.snapshot.replace_source(source_type, source_id)
            except Exception as error:
                self.store.fail_semantic_documents(rows, error)
                log_event(
                    logger,
                    logging.ERROR,
                    "semantic_encode_failed",
                    space_id=space["id"],
                    batch_size=len(rows),
                    error_type=type(error).__name__,
                )
        building = self.store.semantic_space(state="building")
        if building is not None and self.auto_activate:
            status = self.store.semantic_status(str(building["id"]))
            if (
                status["eligible_source_coverage"] >= 1.0
                and not status["pending"]
                and not status["encoding"]
                and not status["retry"]
                and not status["dirty_sources"]
            ):
                self.store.activate_semantic_space(str(building["id"]))
                self.snapshot.load(str(building["id"]))
                self.degraded_reason = ""
                log_event(
                    logger,
                    logging.INFO,
                    "semantic_space_activated",
                    space_id=building["id"],
                    model=building["model"],
                )
        return worked

    async def run_worker(
        self,
        stop: asyncio.Event,
        *,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        if not self.config.enabled:
            await stop.wait()
            return
        if self._needs_reconciliation:
            for state in ("building", "active"):
                space = self.store.semantic_space(state=state)
                if space is not None:
                    self.store.reconcile_semantic_sources(str(space["id"]))
            self._needs_reconciliation = False
        while not stop.is_set():
            try:
                worked = await self.maintain_once(allow_encoding=not busy())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                worked = False
                log_event(
                    logger,
                    logging.ERROR,
                    "semantic_worker_failed",
                    error_type=type(error).__name__,
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.05 if worked else 2.0)
            except TimeoutError:
                pass
