from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterable

from ..integrations.models import EmbeddingSpaceConfig
from ..observability.events import log_event
from ..policies import SemanticPolicy
from ..storage import MemoryRecallQuery, Store
from ..storage.semantic.semantic_documents import DOCUMENT_TEMPLATE_VERSION, QUERY_TEMPLATE_VERSION
from ..storage.episode.episode_ranking import EpisodeRecallQuery
from ..integrations.contracts.embedding import Embedder
from ..integrations.errors import error_category
from .models import (
    CALIBRATION_PROFILES,
    DenseEpisodeHit,
    DenseThresholds,
    DenseRecallEvidence,
)
from ..memory.indexing.worker import IndexWorker
from .indexing import MomoiIndexAdapter
from ..memory.retrieval.snapshot import SegmentedVectorSnapshot
from ..memory.retrieval.dense import DenseQueryService, DenseSearchPool, MemoryVectorRecall

logger = logging.getLogger(__name__)
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class SemanticRecallService:
    def __init__(
        self,
        store: Store,
        config: EmbeddingSpaceConfig,
        *,
        auto_activate: bool = True,
        policy: SemanticPolicy = SemanticPolicy(),
        client: Embedder | None,
    ) -> None:
        self.store = store
        self.config = config
        self.policy = policy
        self.client = client
        if config.enabled and client is None:
            raise ValueError("enabled semantic recall requires an embedder")
        self.snapshot = SegmentedVectorSnapshot(store.memory_vectors, config.dimensions)
        self.queries = DenseQueryService(
            self.snapshot, client, instruction=QUERY_INSTRUCTION,
            candidate_floor=policy.candidate_floor,
            candidate_multiplier=policy.candidate_multiplier,
            unavailable_reason="disabled" if not config.enabled else "no_active_space",
            on_failure=self._query_failure,
        )
        self.memory_dense_recall = MemoryVectorRecall(self.queries, {
            kind: DenseThresholds(*values)
            for kind, values in CALIBRATION_PROFILES.get(config.calibration_profile, {}).items()
        })
        self.index_worker = IndexWorker(
            store.memory_index_queue, client,
            MomoiIndexAdapter(store, self.snapshot, self.queries, auto_activate=auto_activate),
            document_batch_size=config.document_batch_size,
            active_poll_seconds=policy.active_poll_seconds,
            idle_poll_seconds=policy.idle_poll_seconds,
        )
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
            int(space["document_template_version"]) != DOCUMENT_TEMPLATE_VERSION
            or int(space["query_template_version"]) != QUERY_TEMPLATE_VERSION
            or str(space["model"]) != self.config.model
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
            # A document-template upgrade does not invalidate the encoder's
            # coordinate space. Keep the previous index available while rebuilding.
            if (str(space["model"]) == self.config.model
                    and int(space["dimensions"]) == self.config.dimensions
                    and str(space["calibration_profile"]) == self.config.calibration_profile
                    and int(space["query_template_version"]) == QUERY_TEMPLATE_VERSION):
                self.snapshot.load(str(space["id"]))
                self.degraded_reason = ""
            return
        self.snapshot.load(str(space["id"]))
        self._needs_reconciliation = True
        self.degraded_reason = ""

    @property
    def degraded_reason(self) -> str:
        return self.queries.unavailable_reason

    @degraded_reason.setter
    def degraded_reason(self, value: str) -> None:
        self.queries.unavailable_reason = value

    @staticmethod
    def _query_failure(error: Exception, batch_size: int) -> None:
        log_event(
            logger, logging.WARNING, "semantic_query_fallback",
            reason=f"{type(error).__name__}: {str(error)[:160]}",
            error_type=type(error).__name__,
            category="timeout" if error_category(error) == "timeout" else "error",
            query_batch_size=batch_size,
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
        pools = [DenseSearchPool({"confirmed_memory"})] if include_memory else []
        if include_episode:
            episode_types = (
                {"episode_turn"}
                if episode_after is not None or episode_before is not None
                else {"episode_summary", "episode_cue"}
            )
            pools.extend(
                DenseSearchPool({kind}, episode_after, episode_before, group_by_parent=True)
                for kind in episode_types
            )
        result = await self.queries.search(
            (query.dense_expression for query in queries), pools, limit=output_limit,
        )
        episodes: dict[str, dict[str, DenseEpisodeHit]] = {}
        for expression, hits in result.hits.items():
            values: dict[str, dict[str, float]] = {}
            for meta, cosine in hits:
                field_name = {
                    "episode_summary": "summary_cosine",
                    "episode_cue": "cue_cosine",
                    "episode_turn": "turn_cosine",
                }.get(meta.document_type)
                if field_name is None:
                    continue
                fields = values.setdefault(meta.parent_id or meta.source_id, {})
                fields[field_name] = max(cosine, fields.get(field_name, -1.0))
            episodes[expression] = {
                episode_id: DenseEpisodeHit(episode_id, **fields)
                for episode_id, fields in values.items()
            }
        return DenseRecallEvidence(
            space_id=result.space_id, calibration_profile=self.config.calibration_profile,
            memory=result.memory, episodes=episodes, query_batch_size=result.query_batch_size,
            request_ms=result.request_ms, search_ms=result.search_ms,
            fallback_reason=result.fallback_reason,
        )

    async def maintain_once(self, *, allow_encoding: bool = True) -> bool:
        if not self.config.enabled:
            return False
        return await self.index_worker.maintain_once(allow_encoding=allow_encoding)

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
        await self.index_worker.run(stop, busy=busy)
