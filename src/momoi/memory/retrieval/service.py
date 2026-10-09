"""Memory candidate retrieval and ranking, independent of Agent and Episode storage."""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy

from .models import MemoryRecallQuery, MemoryDenseEvidence, MemoryDenseRecall
from .ranking import rank_recall_items
from ..metadata import MemoryFilters
from ..storage.records import MEMORY_ACTIVATIONS
from ..storage.repository import MemoryRepository
from .sparse import (
    SearchBackend, StringSearchBackend, alternative_weights, document_frequency,
    search_alternatives, search_expression,
)
from .rerank import MemoryReranker, validate_reranked_rows

_MEMORY_QUERY_PRIORITY_WEIGHTS = (1.0, 0.5, 0.3)
_MEMORY_SECOND_ALIAS_WEIGHT = 0.18
_MEMORY_THIRD_ALIAS_WEIGHT = 0.08
_MEMORY_SECOND_QUERY_WEIGHT = 0.55
_MEMORY_THIRD_QUERY_WEIGHT = 0.2
_MEMORY_RECENCY_FLOOR = 0.8
_MEMORY_RECENCY_HALF_LIFE_SECONDS = 180 * 86400
_CONFIRMED_MEMORY_SCORE_FLOOR = 0.35
_REFLECTION_MEMORY_SCORE_FLOOR = 0.35
MAX_MEMORY_RECALL_CANDIDATES = 24
MAX_MEMORY_RECALL_RESULTS = 6


class MemoryRecallService:
    def __init__(
        self, repository: MemoryRepository, search_backend: SearchBackend | None = None, *,
        reflection_rows: Callable[[], Sequence[Mapping[str, object]]] | None = None,
        dense_recall: MemoryDenseRecall | None = None,
        reranker: MemoryReranker | None = None,
    ) -> None:
        self.repository = repository
        self._search_backend = search_backend or StringSearchBackend()
        self._reflection_rows = reflection_rows
        self.dense_recall = dense_recall
        self._reranker = reranker

    async def search(
        self, query: str | list[MemoryRecallQuery], limit: int = 6, *,
        filters: MemoryFilters | None = None,
        request: str | None = None, dense_evidence: MemoryDenseEvidence | None = None,
        reranker: MemoryReranker | None = None,
    ) -> list[dict[str, object]]:
        """Retrieve and rerank confirmed memories; model capabilities are injected.

        A supplied reranker also runs for an empty pool: a joint adapter may
        still have other evidence to select in the same model request.
        """
        queries = [MemoryRecallQuery(query.strip())] if isinstance(query, str) else list(query)
        if not queries or any(not item.dense_expression for item in queries):
            raise ValueError("memory search requires a nonempty query")
        filters = self.repository.tags.filters(filters)
        if dense_evidence is not None and any(filters.values()):
            raise ValueError("filtered search must generate its own dense evidence before top-k")
        reranker = reranker or self._reranker
        limit = min(MAX_MEMORY_RECALL_RESULTS, max(0, limit))
        candidate_limit = MAX_MEMORY_RECALL_CANDIDATES if reranker and limit else limit
        if dense_evidence is None and self.dense_recall is not None and limit > 0:
            dense_evidence = await self.dense_recall(
                queries, candidate_limit, eligible_ids=self.eligible_source_ids(queries, filters),
            )
        candidates = self.rank(queries, candidate_limit, dense_evidence=dense_evidence, filters=filters)
        if reranker is None:
            return candidates
        request = request if request is not None else "\n".join(q.dense_expression for q in queries)
        # A transport must not be able to mutate the validated candidate pool.
        selected = await reranker(request, deepcopy(candidates))
        return validate_reranked_rows(candidates, selected)[:limit]

    def eligible_source_ids(self, queries, filters: MemoryFilters | None = None):
        """Current visibility and metadata constrain vectors before truncation."""
        rows = self.repository.recall_rows(now=time.time(), filters=filters)
        eligible = {}
        for query in queries:
            ids = eligible.setdefault(query.dense_expression, set())
            ids.update(str(row["id"]) for row in rows
                       if not query.kinds or row["kind"] in query.kinds)
        return {expression: frozenset(ids) for expression, ids in eligible.items()}

    def _alternative_weights(
        self,
        query: str,
        documents: list[tuple[str, ...]],
    ) -> dict[str, float]:
        return alternative_weights(
            document_frequency(
                search_alternatives(query), documents, self._search_backend
            ),
            len(documents),
        )

    def rank(
        self,
        queries: list[MemoryRecallQuery],
        max_results: int,
        *,
        now: float | None = None,
        include_reflections: bool = False,
        dense_evidence: MemoryDenseEvidence | None = None,
        filters: MemoryFilters | None = None,
    ) -> list[dict[str, object]]:
        """Recall confirmed facts; daily observations are opt-in for reflection only."""

        filters = self.repository.tags.filters(filters)
        if max_results <= 0 or not queries:
            return []
        limit = min(MAX_MEMORY_RECALL_CANDIDATES, max_results)
        stamp = time.time() if now is None else now
        confirmed_rows = self.repository.recall_rows(now=stamp, filters=filters)
        reflection_rows = (
            self._reflection_rows() if include_reflections and self._reflection_rows else []
        )

        reflection_rows = [row for row in reflection_rows if not filters["scope"] and not filters["tags_any"]
                           and (not filters["kinds"] or row["kind"] in filters["kinds"])]
        confirmed_candidates: list[dict[str, object]] = []
        for row in confirmed_rows:
            importance = min(1.0, max(0.0, float(row["importance"])))
            confirmed_candidates.append(
                {
                    "source": "confirmed",
                    "id": int(row["id"]),
                    "kind": str(row["kind"]),
                    "key": str(row["key"]),
                    "content": str(row["content"]),
                    "meta": row["meta"],
                    "confidence": 1.0,
                    "reliability_bonus": 0.12 + 0.03 * importance,
                    "recency_floor": 1.0,
                    "updated_at": float(row["updated_at"]),
                }
            )
        reflection_candidates: list[dict[str, object]] = []
        for row in reflection_rows:
            confidence = min(1.0, max(0.0, float(row["confidence"])))
            reflection_candidates.append(
                {
                    "source": "reflection",
                    "id": int(row["id"]),
                    "kind": str(row["kind"]),
                    "key": str(row["key"]),
                    "content": str(row["content"]),
                    "local_date": str(row["local_date"] or "unknown"),
                    "confidence": confidence,
                    "evidence": str(row["evidence"]),
                    "reliability_bonus": 0.06 * confidence,
                    "recency_floor": _MEMORY_RECENCY_FLOOR,
                    "updated_at": float(row["updated_at"]),
                }
            )
        return [
            *self._rank_memory_pool(
                queries,
                confirmed_candidates,
                limit,
                _CONFIRMED_MEMORY_SCORE_FLOOR,
                stamp,
                dense_evidence,
            ),
            *self._rank_memory_pool(
                queries,
                reflection_candidates,
                limit,
                _REFLECTION_MEMORY_SCORE_FLOOR,
                stamp,
                dense_evidence,
            ),
        ]

    def _rank_memory_pool(
        self,
        queries: list[MemoryRecallQuery],
        candidates: list[dict[str, object]],
        limit: int,
        score_floor: float,
        now: float,
        dense_evidence: MemoryDenseEvidence | None = None,
    ) -> list[dict[str, object]]:
        if not candidates or limit <= 0:
            return []
        documents = [
            (str(item["key"]), str(item["content"])) for item in candidates
        ]
        states: list[dict[str, object]] = [
            {
                "unit_scores": {},
                "eligibility_scores": [],
                "evidence_signals": [],
                "matched_queries": [],
                "unit_ids": set(),
                "channels": set(),
                "dense_cosines": [],
                "agreement_bonus": 0.0,
            }
            for _ in candidates
        ]
        for query in queries:
            weights = self._alternative_weights(query.expression, documents)
            priority_weight = _MEMORY_QUERY_PRIORITY_WEIGHTS[
                min(
                    max(0, int(query.priority)),
                    len(_MEMORY_QUERY_PRIORITY_WEIGHTS) - 1,
                )
            ]
            for index, document in enumerate(documents):
                allowed_kinds = set(query.kinds)
                if allowed_kinds and str(candidates[index]["kind"]) not in allowed_kinds:
                    continue
                match = search_expression(
                    query.expression,
                    document,
                    self._search_backend,
                    weights=weights,
                )
                document_type = (
                    "confirmed_memory"
                    if candidates[index]["source"] == "confirmed"
                    else "reflection_memory"
                )
                dense_hit = (
                    dense_evidence.memory.get(query.dense_expression, {}).get(
                        (document_type, str(candidates[index]["id"]))
                    )
                    if dense_evidence is not None
                    else None
                )
                thresholds = (
                    dense_evidence.thresholds(document_type)
                    if dense_evidence is not None else None
                )
                # Confirmed memory uses one semantic candidate floor before hybrid
                # ranking. Known low-similarity hits cannot be rescued by generic words.
                # Records awaiting indexing retain sparse recall.
                semantic_gate = (
                    document_type == "confirmed_memory" and thresholds is not None and dense_hit is not None
                    and not getattr(dense_evidence, "fallback_reason", "")
                )
                if semantic_gate and dense_hit.cosine < thresholds.only:
                    continue
                if match is None and dense_hit is None:
                    continue
                alias_scores = (
                    sorted(
                        (
                            float(weights.get(alternative, 1.0))
                            for alternative in match.alternatives
                        ),
                        reverse=True,
                    )
                    if match is not None
                    else []
                )
                sparse_score = alias_scores[0] if alias_scores else 0.0
                if len(alias_scores) > 1:
                    sparse_score += _MEMORY_SECOND_ALIAS_WEIGHT * alias_scores[1]
                if len(alias_scores) > 2:
                    sparse_score += _MEMORY_THIRD_ALIAS_WEIGHT * alias_scores[2]
                cosine = float(dense_hit.cosine) if dense_hit is not None else None
                dense_score = (
                    thresholds.calibrated(cosine)
                    if thresholds is not None and cosine is not None
                    else 0.0
                )
                normalized_sparse = 1.0 - math.exp(-sparse_score)
                agreement = (
                    min(normalized_sparse, dense_score)
                    if sparse_score > 0 and dense_score > 0
                    else 0.0
                )
                hybrid_score = sparse_score + 0.55 * dense_score + 0.20 * agreement
                score = hybrid_score * priority_weight
                state = states[index]
                eligibility_scores = state["eligibility_scores"]
                assert isinstance(eligibility_scores, list)
                if sparse_score > 0:
                    eligibility_scores.append(sparse_score)
                signals = state["evidence_signals"]
                assert isinstance(signals, list)
                signals.append((sparse_score, cosine, hybrid_score, thresholds))
                unit_scores = state["unit_scores"]
                assert isinstance(unit_scores, dict)
                units = query.unit_ids or (f"query:{query.dense_expression}",)
                for unit_id in units:
                    unit_scores.setdefault(unit_id, []).append(score)
                matched_queries = state["matched_queries"]
                assert isinstance(matched_queries, list)
                matched_queries.append(query.dense_expression)
                unit_ids = state["unit_ids"]
                assert isinstance(unit_ids, set)
                unit_ids.update(query.unit_ids)
                channels = state["channels"]
                assert isinstance(channels, set)
                if sparse_score > 0:
                    channels.add("sparse")
                if cosine is not None:
                    channels.add("dense")
                    dense_cosines = state["dense_cosines"]
                    assert isinstance(dense_cosines, list)
                    dense_cosines.append(cosine)
                state["agreement_bonus"] = float(state["agreement_bonus"]) + 0.20 * agreement

        ranked_candidates: list[dict[str, object]] = []
        for candidate, state in zip(candidates, states, strict=True):
            unit_scores = state["unit_scores"]
            assert isinstance(unit_scores, dict)
            if not unit_scores:
                continue
            semantic_score = 0.0
            for scores in unit_scores.values():
                ordered = sorted((float(value) for value in scores), reverse=True)
                semantic_score += ordered[0]
                if len(ordered) > 1:
                    semantic_score += _MEMORY_SECOND_QUERY_WEIGHT * ordered[1]
                if len(ordered) > 2:
                    semantic_score += _MEMORY_THIRD_QUERY_WEIGHT * ordered[2]
            if len(unit_scores) > 1:
                semantic_score *= 1.0 + min(0.3, 0.1 * (len(unit_scores) - 1))

            updated_at = float(candidate["updated_at"])
            age = max(0.0, now - updated_at)
            recency_floor = float(candidate["recency_floor"])
            recency_factor = recency_floor + (
                1.0 - recency_floor
            ) * math.exp(
                -math.log(2.0)
                * age
                / _MEMORY_RECENCY_HALF_LIFE_SECONDS
            )
            search_score = (
                semantic_score * recency_factor
                + float(candidate["reliability_bonus"])
            )
            eligibility_scores = state["eligibility_scores"]
            assert isinstance(eligibility_scores, list)
            sparse_eligibility = (
                max(float(value) for value in eligibility_scores)
                * recency_factor
                + float(candidate["reliability_bonus"])
                if eligibility_scores
                else 0.0
            )
            signals = state["evidence_signals"]
            assert isinstance(signals, list)
            dense_admitted = False
            support_admitted = False
            hybrid_eligibility = sparse_eligibility
            for sparse_score, cosine, hybrid_score, thresholds in signals:
                if cosine is None or thresholds is None:
                    continue
                if cosine >= thresholds.only and (candidate["source"] == "confirmed" or sparse_score <= 0):
                    dense_admitted = True
                if (
                    sparse_score > 0
                    and cosine >= thresholds.support
                    and hybrid_score * recency_factor
                    + float(candidate["reliability_bonus"]) >= score_floor
                ):
                    support_admitted = True
                hybrid_eligibility = max(
                    hybrid_eligibility,
                    hybrid_score * recency_factor
                    + float(candidate["reliability_bonus"]),
                )
            if (
                sparse_eligibility < score_floor
                and not dense_admitted
                and not support_admitted
            ):
                continue
            channels = state["channels"]
            assert isinstance(channels, set)
            dense_cosines = state["dense_cosines"]
            assert isinstance(dense_cosines, list)
            ranked_candidates.append(
                {
                    **{
                        key: value
                        for key, value in candidate.items()
                        if key not in {"reliability_bonus", "recency_floor"}
                    },
                    "semantic_score": semantic_score,
                    "search_score": search_score,
                    "eligibility_score": hybrid_eligibility,
                    "score_floor": score_floor,
                    "last_activity_at": updated_at,
                    "matched_queries": list(
                        dict.fromkeys(str(value) for value in state["matched_queries"])
                    ),
                    "unit_ids": sorted(str(value) for value in state["unit_ids"]),
                    "channels": sorted(str(value) for value in channels),
                    "dense_cosine": max(dense_cosines) if dense_cosines else None,
                    "agreement_bonus": float(state["agreement_bonus"]),
                    "dense_only": dense_admitted and not eligibility_scores,
                }
            )
        return rank_recall_items(ranked_candidates, now=now)[:limit]

    def search_literal(
        self,
        query: str,
        max_results: int,
        *,
        include_core: bool = False,
        activation: str | None = None,
        include_scoped: bool = False,
        filters: MemoryFilters | None = None,
    ) -> list[dict[str, object]]:
        self.repository.tags.filters(filters)
        if max_results <= 0:
            return []
        if activation is not None and activation not in MEMORY_ACTIVATIONS:
            raise ValueError("invalid memory activation")
        rows = self.repository.search_rows(
            activation=activation, include_scoped=include_scoped, filters=filters,
        )
        core_kinds = {"profile", "relationship", "shared"}
        ranked: list[tuple[float, object]] = []
        documents = [(str(row["key"]), str(row["content"])) for row in rows]
        weights = self._alternative_weights(query, documents)
        for row, document in zip(rows, documents):
            match = search_expression(
                query,
                document,
                self._search_backend,
                weights=weights,
            )
            core = include_core and row["kind"] in core_kinds
            if not core and match is None:
                continue
            score = (
                (match.score if match else 0.0)
                + float(row["importance"]) * 0.1
                + (1.0 if core else 0.0)
            )
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]
