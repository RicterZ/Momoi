from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Iterable

import numpy as np

from ..config.models import EmbeddingConfig
from ..logging_context import log_event
from ..policies import SemanticPolicy
from ..storage import MemoryRecallQuery, Store
from ..storage.episode_ranking import EpisodeRecallQuery
from .client import EmbeddingClient, semantic_error_category
from .models import (
    CALIBRATION_PROFILES,
    DenseEpisodeHit,
    DenseMemoryHit,
    DenseRecallEvidence,
)
from .snapshot import SegmentedVectorSnapshot, VectorMetadata

logger = logging.getLogger(__name__)
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class SemanticRecallService:
    def __init__(
        self,
        store: Store,
        config: EmbeddingConfig,
        *,
        auto_activate: bool = True,
        policy: SemanticPolicy = SemanticPolicy(),
    ) -> None:
        self.store = store
        self.config = config
        self.policy = policy
        self.client = EmbeddingClient(config, policy)
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
                query.dense_expression for query in queries if query.dense_expression
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
        candidate_width = max(
            self.policy.candidate_floor,
            max(1, output_limit) * self.policy.candidate_multiplier,
        )
        search_started = time.monotonic()
        memory_hits: dict[int, list[tuple[VectorMetadata, float]]] = {}
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
        episode_hits: dict[int, list[tuple[VectorMetadata, float]]] = {}
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
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=(
                        self.policy.active_poll_seconds
                        if worked
                        else self.policy.idle_poll_seconds
                    ),
                )
            except TimeoutError:
                pass
