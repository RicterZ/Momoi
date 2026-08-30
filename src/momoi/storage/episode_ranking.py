from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .episode_search import (
    EpisodeQueryMatches,
    EpisodeSearchDocument,
    EpisodeSearchHit,
    EpisodeSearchMessage,
)

if TYPE_CHECKING:
    from ..semantic import DenseRecallEvidence


_QUERY_PRIORITY_WEIGHTS = (1.0, 0.5, 0.3)
_SECOND_ALIAS_WEIGHT = 0.18
_THIRD_ALIAS_WEIGHT = 0.08
_SECOND_QUERY_WEIGHT = 0.55
_THIRD_QUERY_WEIGHT = 0.2
_SPARSE_SCORE_SATURATION = 2.0
_RECENCY_FLOOR = 0.8
_RECENCY_HALF_LIFE_SECONDS = 180 * 86400
_RELEVANCE_CONFIDENCE_FLOOR = 0.47
_CONFIDENCE_SCOPED_EXPRESSION_MATCH_WEIGHT = 0.36
_CONFIDENCE_QUERY_EXPRESSION_MATCH_WEIGHT = 0.28
_CONFIDENCE_REPEATED_MESSAGE_EXPRESSION_MATCH_WEIGHT = 0.20
_CONFIDENCE_MESSAGE_ONLY_EXPRESSION_MATCH_WEIGHT = 0.12
_CONFIDENCE_TURN_SUPPORT_WEIGHT = 0.06
_CONFIDENCE_FIELD_EVIDENCE_WEIGHT = 0.15
_CONFIDENCE_QUERY_MATCH_WEIGHT = 0.10
_MIN_SELECTIVE_LITERAL_CHARS = 3
_SHORT_LITERAL_TURN_SUPPORT = 3
_SHORT_LITERAL_CONFIDENCE_CEILING = _RELEVANCE_CONFIDENCE_FLOOR - 0.001
_FIELD_WEIGHTS = {
    "title": 3.0,
    "topic": 2.7,
    "entity": 2.7,
    "open_loop": 2.4,
    "scoped": 2.2,
    "narrative_summary": 2.2,
    "working_summary": 1.7,
    "summary": 1.7,
}
_SELECTIVE_FIELDS = {"title", "topic", "entity", "open_loop"}


@dataclass(frozen=True)
class EpisodeRecallQuery:
    expression: str
    unit_ids: tuple[str, ...] = ()
    priority: int = 0
    semantic_expression: str = ""

    @property
    def dense_expression(self) -> str:
        return self.semantic_expression.strip() or self.expression.strip()


@dataclass(frozen=True)
class EpisodeRankedQuery:
    expression: str
    unit_ids: tuple[str, ...]
    priority: int
    score: float
    matched_alternatives: tuple[str, ...]
    alternative_count: int
    field_matches: tuple[str, ...]
    message_ids: tuple[int, ...]
    scoped_message_ids: tuple[int, ...]
    turn_ids: tuple[str, ...]


@dataclass(frozen=True)
class RankedEpisodeHit:
    episode_id: str
    score: float
    semantic_score: float
    relevance_confidence: float
    last_activity_at: float
    salience: float
    matches: tuple[EpisodeSearchMessage, ...]
    matched_keywords: tuple[str, ...]
    matched_queries: tuple[EpisodeRankedQuery, ...]
    channels: tuple[str, ...] = ()
    dense_cosine: float | None = None
    agreement_bonus: float = 0.0
    corroboration_bonus: float = 0.0
    dense_only: bool = False


def _idf(document_count: int, document_frequency: int) -> float:
    if document_count <= 0 or document_frequency <= 0:
        return 0.0
    raw = math.log1p(
        (document_count - document_frequency + 0.5)
        / (document_frequency + 0.5)
    )
    ceiling = math.log1p(document_count + 0.5)
    return raw / ceiling if ceiling > 0 else 0.0


def _message_weight(message: EpisodeSearchMessage) -> float:
    if message.scoped:
        weight = 2.2
    elif message.role == "user":
        weight = 1.55
    elif message.role == "event":
        weight = 0.8
    elif message.delivery_state == "internal":
        weight = 1.15
    else:
        weight = 0.85
    if message.relation != "primary":
        weight *= 0.75
    return weight


def _hit_strength(hit: EpisodeSearchHit) -> float:
    signals = [
        *(_FIELD_WEIGHTS.get(field, 1.0) for field in hit.field_matches),
        *(_message_weight(message) for message in hit.matches),
    ]
    if not signals:
        return 0.0
    signals.sort(reverse=True)
    strongest = signals[0]
    corroboration = 0.18 * sum(signals[1:3])
    density = 0.08 * min(
        math.log1p(max(0, hit.message_match_count - 1)),
        math.log(5),
    )
    turn_support = 0.20 * min(
        math.log1p(max(0, hit.distinct_turn_count - 1)),
        math.log(4),
    )
    return strongest + corroboration + density + turn_support


def _priority_weight(priority: int) -> float:
    return _QUERY_PRIORITY_WEIGHTS[
        min(max(0, priority), len(_QUERY_PRIORITY_WEIGHTS) - 1)
    ]


def _saturate_sparse_score(score: float) -> float:
    """Keep stronger sparse evidence useful without letting it dominate rank."""
    if score <= 0:
        return 0.0
    return -_SPARSE_SCORE_SATURATION * math.expm1(
        -score / _SPARSE_SCORE_SATURATION
    )


def _query_score(
    query: EpisodeRecallQuery,
    matches: EpisodeQueryMatches,
    document_count: int,
) -> dict[str, tuple[float, list[EpisodeSearchHit]]]:
    by_episode: dict[str, list[tuple[float, EpisodeSearchHit]]] = {}
    for alternative in matches.alternatives:
        frequency = len(alternative.hits)
        weight = _idf(document_count, frequency)
        for hit in alternative.hits:
            score = weight * _hit_strength(hit)
            if score <= 0:
                continue
            by_episode.setdefault(hit.episode_id, []).append((score, hit))

    results: dict[str, tuple[float, list[EpisodeSearchHit]]] = {}
    for episode_id, scored_hits in by_episode.items():
        scored_hits.sort(key=lambda item: item[0], reverse=True)
        scores = [score for score, _ in scored_hits]
        combined = scores[0]
        if len(scores) > 1:
            combined += _SECOND_ALIAS_WEIGHT * scores[1]
        if len(scores) > 2:
            combined += _THIRD_ALIAS_WEIGHT * scores[2]
        results[episode_id] = (combined, [hit for _, hit in scored_hits])
    return results


def _query_relevance_confidence(
    matched_query: EpisodeRankedQuery,
) -> float:
    """Measure whether one retrieval need has enough supporting evidence.

    Query order, cross-query coverage, and matching additional parallel aliases
    are ranking preferences.  They must not make an Episode that strongly
    satisfies one independent retrieval need ineligible merely because it does
    not also satisfy the other needs or aliases emitted for the same intent.
    """

    field_values = sorted(
        (
            min(1.0, _FIELD_WEIGHTS.get(field, 1.0) / 3.0)
            for field in matched_query.field_matches
        ),
        reverse=True,
    )
    field_evidence = min(
        1.0,
        (field_values[0] if field_values else 0.0)
        + 0.12 * sum(field_values[1:3]),
    )
    turn_count = len(set(matched_query.turn_ids))
    turn_support = min(1.0, math.log1p(turn_count) / math.log(4.0))
    scoped_support = bool(matched_query.scoped_message_ids)
    if scoped_support:
        expression_match_weight = _CONFIDENCE_SCOPED_EXPRESSION_MATCH_WEIGHT
    elif field_values:
        expression_match_weight = _CONFIDENCE_QUERY_EXPRESSION_MATCH_WEIGHT
    elif turn_count > 1:
        expression_match_weight = (
            _CONFIDENCE_REPEATED_MESSAGE_EXPRESSION_MATCH_WEIGHT
        )
    else:
        expression_match_weight = _CONFIDENCE_MESSAGE_ONLY_EXPRESSION_MATCH_WEIGHT
    confidence = (
        expression_match_weight
        + _CONFIDENCE_TURN_SUPPORT_WEIGHT * turn_support
        + _CONFIDENCE_FIELD_EVIDENCE_WEIGHT * field_evidence
        + _CONFIDENCE_QUERY_MATCH_WEIGHT
    )
    selective_literal = any(
        sum(character.isalnum() for character in alternative)
        >= _MIN_SELECTIVE_LITERAL_CHARS
        for alternative in matched_query.matched_alternatives
    )
    selective_support = bool(
        _SELECTIVE_FIELDS.intersection(matched_query.field_matches)
    ) or turn_count >= _SHORT_LITERAL_TURN_SUPPORT or (
        scoped_support and matched_query.alternative_count == 1
    )
    if not selective_literal and not selective_support:
        # A very short exact literal found only in prose is ambiguous even when
        # generated summaries repeat it. Keep it below the eligibility floor;
        # title/topic/entity evidence or recurrence across Turns can establish it.
        confidence = min(confidence, _SHORT_LITERAL_CONFIDENCE_CEILING)
    return confidence


def _relevance_confidence(
    matched_queries: tuple[EpisodeRankedQuery, ...],
) -> float:
    """Return the strongest independently supported retrieval need.

    The semantic score below still rewards priority and evidence spanning
    multiple queries.  Eligibility is intentionally the maximum per-query
    confidence so unrelated retrieval needs cannot veto one another.
    """

    return max(
        (
            _query_relevance_confidence(query)
            for query in matched_queries
        ),
        default=0.0,
    )


def rank_episode_matches(
    queries: list[EpisodeRecallQuery],
    matches: list[EpisodeQueryMatches],
    documents: list[EpisodeSearchDocument],
    *,
    limit: int = 8,
    offset: int = 0,
    now: float | None = None,
    minimum_confidence: float = _RELEVANCE_CONFIDENCE_FLOOR,
    dense_evidence: DenseRecallEvidence | None = None,
) -> list[RankedEpisodeHit]:
    if limit <= 0 or offset < 0 or minimum_confidence < 0 or not queries:
        return []
    if len(queries) != len(matches):
        raise ValueError("query and match counts differ")
    now = time.time() if now is None else now
    episodes: dict[str, dict[str, object]] = {}
    for query_index, (query, query_matches) in enumerate(
        zip(queries, matches, strict=True)
    ):
        sparse_by_episode = _query_score(
            query,
            query_matches,
            len(documents),
        )
        dense_by_episode = (
            dense_evidence.episodes.get(query.dense_expression, {})
            if dense_evidence is not None
            else {}
        )
        document_by_id = {document.episode_id: document for document in documents}
        for episode_id in set(sparse_by_episode).union(dense_by_episode):
            sparse_score, hits = sparse_by_episode.get(episode_id, (0.0, []))
            dense_hit = dense_by_episode.get(episode_id)
            document = document_by_id.get(episode_id)
            if document is None:
                continue
            summary_thresholds = (
                dense_evidence.thresholds("episode_summary")
                if dense_evidence is not None
                else None
            )
            turn_thresholds = (
                dense_evidence.thresholds("episode_turn")
                if dense_evidence is not None
                else None
            )
            summary_cosine = (
                dense_hit.summary_cosine if dense_hit is not None else None
            )
            turn_cosine = dense_hit.turn_cosine if dense_hit is not None else None
            summary_dense = (
                summary_thresholds.calibrated(summary_cosine)
                if summary_thresholds is not None and summary_cosine is not None
                else 0.0
            )
            turn_dense = (
                turn_thresholds.calibrated(turn_cosine)
                if turn_thresholds is not None and turn_cosine is not None
                else 0.0
            )
            dense_score = max(summary_dense, turn_dense)
            normalized_sparse = 1.0 - math.exp(-sparse_score)
            sparse_component = _saturate_sparse_score(sparse_score)
            agreement = (
                min(normalized_sparse, dense_score)
                if sparse_score > 0 and dense_score > 0
                else 0.0
            )
            corroboration = (
                0.10 * min(summary_dense, turn_dense)
                if summary_dense > 0 and turn_dense > 0
                else 0.0
            )
            hybrid_score = (
                sparse_component
                + 0.55 * dense_score
                + 0.20 * agreement
                + corroboration
            ) * _priority_weight(query.priority)
            state = episodes.setdefault(
                episode_id,
                {
                    "queries": [],
                    "unit_scores": {},
                    "matches": {},
                    "keywords": set(),
                    "last_activity_at": 0.0,
                    "salience": 0.0,
                    "eligibility": [],
                    "channels": set(),
                    "dense_cosines": [],
                    "agreement_bonus": 0.0,
                    "corroboration_bonus": 0.0,
                },
            )
            field_matches = {
                field for hit in hits for field in hit.field_matches
            }
            message_matches = {
                message.id: message for hit in hits for message in hit.matches
            }
            alternatives = tuple(
                dict.fromkeys(hit.alternative for hit in hits)
            )
            query_evidence = EpisodeRankedQuery(
                expression=query.dense_expression,
                unit_ids=query.unit_ids,
                priority=query.priority,
                score=hybrid_score,
                matched_alternatives=alternatives,
                alternative_count=len(query_matches.alternatives),
                field_matches=tuple(sorted(field_matches)),
                message_ids=tuple(sorted(message_matches)),
                scoped_message_ids=tuple(
                    sorted(
                        message.id
                        for message in message_matches.values()
                        if message.scoped
                    )
                ),
                turn_ids=tuple(
                    sorted({message.turn_id for message in message_matches.values()})
                ),
            )
            query_rows = state["queries"]
            assert isinstance(query_rows, list)
            query_rows.append((query_index, query_evidence))
            unit_scores = state["unit_scores"]
            assert isinstance(unit_scores, dict)
            units = query.unit_ids or (f"query:{query_index}",)
            for unit_id in units:
                unit_scores.setdefault(unit_id, []).append(hybrid_score)
            all_matches = state["matches"]
            assert isinstance(all_matches, dict)
            all_matches.update(message_matches)
            keywords = state["keywords"]
            assert isinstance(keywords, set)
            keywords.update(alternatives)
            state["last_activity_at"] = max(
                float(state["last_activity_at"]),
                max((hit.last_activity_at for hit in hits), default=document.last_activity_at),
            )
            state["salience"] = max(
                float(state["salience"]),
                max((hit.salience for hit in hits), default=document.salience),
            )
            query_confidence = _query_relevance_confidence(query_evidence)
            raw_cosine = max(
                (value for value in (summary_cosine, turn_cosine) if value is not None),
                default=None,
            )
            threshold_pairs = tuple(
                (float(cosine), threshold)
                for cosine, threshold in (
                    (summary_cosine, summary_thresholds),
                    (turn_cosine, turn_thresholds),
                )
                if cosine is not None and threshold is not None
            )
            eligibility = state["eligibility"]
            assert isinstance(eligibility, list)
            eligibility.append(
                {
                    "sparse": sparse_score > 0,
                    "sparse_confidence": query_confidence,
                    "cosine": raw_cosine,
                    "dense_only_pass": any(
                        cosine >= threshold.only
                        for cosine, threshold in threshold_pairs
                    ),
                    "support_pass": any(
                        cosine >= threshold.support
                        for cosine, threshold in threshold_pairs
                    ),
                    "hybrid_confidence": min(1.0, query_confidence + 0.20 * dense_score),
                }
            )
            channels = state["channels"]
            assert isinstance(channels, set)
            if sparse_score > 0:
                channels.add("sparse")
            if raw_cosine is not None:
                channels.add("dense")
                dense_cosines = state["dense_cosines"]
                assert isinstance(dense_cosines, list)
                dense_cosines.append(raw_cosine)
            state["agreement_bonus"] = float(state["agreement_bonus"]) + 0.20 * agreement
            state["corroboration_bonus"] = float(state["corroboration_bonus"]) + corroboration

    ranked: list[RankedEpisodeHit] = []
    for episode_id, state in episodes.items():
        unit_scores = state["unit_scores"]
        assert isinstance(unit_scores, dict)
        semantic_score = 0.0
        for scores in unit_scores.values():
            ordered = sorted((float(value) for value in scores), reverse=True)
            semantic_score += ordered[0]
            if len(ordered) > 1:
                semantic_score += _SECOND_QUERY_WEIGHT * ordered[1]
            if len(ordered) > 2:
                semantic_score += _THIRD_QUERY_WEIGHT * ordered[2]
        if len(unit_scores) > 1:
            semantic_score *= 1.0 + min(0.3, 0.1 * (len(unit_scores) - 1))
        last_activity_at = float(state["last_activity_at"])
        age = max(0.0, now - last_activity_at)
        recency_factor = _RECENCY_FLOOR + (1.0 - _RECENCY_FLOOR) * math.exp(
            -math.log(2.0) * age / _RECENCY_HALF_LIFE_SECONDS
        )
        salience = min(1.0, max(0.0, float(state["salience"])))
        score = semantic_score * recency_factor + 0.05 * salience
        all_matches = state["matches"]
        assert isinstance(all_matches, dict)
        ordered_matches = tuple(
            sorted(
                all_matches.values(),
                key=lambda message: (message.ordinal, message.id),
                reverse=True,
            )[:4]
        )
        query_rows = state["queries"]
        assert isinstance(query_rows, list)
        ranked_queries = tuple(
            evidence
            for _, evidence in sorted(query_rows, key=lambda item: item[0])
        )
        relevance_confidence = _relevance_confidence(ranked_queries)
        eligibility = state["eligibility"]
        assert isinstance(eligibility, list)
        sparse_admitted = relevance_confidence >= minimum_confidence
        dense_only_admitted = any(
            not bool(item["sparse"])
            and bool(item["dense_only_pass"])
            for item in eligibility
        )
        support_admitted = any(
            bool(item["sparse"])
            and bool(item["support_pass"])
            and float(item["hybrid_confidence"]) >= minimum_confidence
            for item in eligibility
        )
        keywords = state["keywords"]
        assert isinstance(keywords, set)
        ranked.append(
            RankedEpisodeHit(
                episode_id=episode_id,
                score=score,
                semantic_score=semantic_score,
                relevance_confidence=relevance_confidence,
                last_activity_at=last_activity_at,
                salience=salience,
                matches=ordered_matches,
                matched_keywords=tuple(sorted(str(value) for value in keywords)),
                matched_queries=ranked_queries,
                channels=tuple(sorted(str(value) for value in state["channels"])),
                dense_cosine=(
                    max(float(value) for value in state["dense_cosines"])
                    if state["dense_cosines"]
                    else None
                ),
                agreement_bonus=float(state["agreement_bonus"]),
                corroboration_bonus=float(state["corroboration_bonus"]),
                dense_only=dense_only_admitted and not any(
                    bool(item["sparse"]) for item in eligibility
                ),
            )
        )
        # Keep admission separate from ranking. Stash the decision without
        # changing the public immutable result shape.
        state["admitted"] = sparse_admitted or dense_only_admitted or support_admitted
    ranked.sort(
        key=lambda hit: (
            hit.score,
            hit.semantic_score,
            hit.last_activity_at,
            hit.episode_id,
        ),
        reverse=True,
    )
    admitted_ids = {
        episode_id for episode_id, state in episodes.items() if state.get("admitted")
    }
    relevant = [hit for hit in ranked if hit.episode_id in admitted_ids]
    return relevant[offset : offset + limit]


def recall_item_score(item: dict[str, object], *, now: float | None = None) -> float:
    relevance = float(item.get("search_score") or 0.0)
    if relevance > 0:
        return relevance
    now = time.time() if now is None else now
    last_activity = float(item.get("last_activity_at") or 0.0)
    age = max(0.0, now - last_activity) if last_activity else 365 * 86400
    recency = 0.08 * math.exp(-age / (30 * 86400))
    salience = min(1.0, max(0.0, float(item.get("salience") or 0.0)))
    return recency + 0.05 * salience


def rank_recall_items(
    items: list[dict[str, object]],
    *,
    now: float | None = None,
) -> list[dict[str, object]]:
    stamp = time.time() if now is None else now
    return sorted(
        items,
        key=lambda item: (
            recall_item_score(item, now=stamp),
            float(item.get("last_activity_at") or 0.0),
            str(item.get("turn_id") or item.get("episode_id") or item.get("id") or ""),
        ),
        reverse=True,
    )
