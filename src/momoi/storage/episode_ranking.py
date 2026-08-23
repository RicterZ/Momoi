from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass

from .episode_search import (
    EpisodeQueryMatches,
    EpisodeSearchDocument,
    EpisodeSearchHit,
    EpisodeSearchMessage,
)


_QUERY_PRIORITY_WEIGHTS = (1.0, 0.5, 0.3)
_SECOND_ALIAS_WEIGHT = 0.18
_THIRD_ALIAS_WEIGHT = 0.08
_SECOND_QUERY_WEIGHT = 0.55
_THIRD_QUERY_WEIGHT = 0.2
_CONTEXT_ALIGNMENT_WEIGHT = 3.0
_RECENCY_FLOOR = 0.8
_RECENCY_HALF_LIFE_SECONDS = 180 * 86400
_RELEVANCE_CONFIDENCE_FLOOR = 0.47
_CONFIDENCE_CONTEXT_COVERAGE_WEIGHT = 0.30
_CONFIDENCE_CONTEXT_ALIGNMENT_WEIGHT = 0.18
_CONFIDENCE_ALIAS_COVERAGE_WEIGHT = 0.24
_CONFIDENCE_TURN_SUPPORT_WEIGHT = 0.06
_CONFIDENCE_FIELD_EVIDENCE_WEIGHT = 0.12
_CONFIDENCE_QUERY_COVERAGE_WEIGHT = 0.10
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


@dataclass(frozen=True)
class EpisodeRecallQuery:
    expression: str
    unit_ids: tuple[str, ...] = ()
    priority: int = 0
    context: str = ""


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
    turn_ids: tuple[str, ...]


@dataclass(frozen=True)
class RankedEpisodeHit:
    episode_id: str
    score: float
    semantic_score: float
    context_score: float
    context_coverage: float
    relevance_confidence: float
    last_activity_at: float
    salience: float
    matches: tuple[EpisodeSearchMessage, ...]
    matched_keywords: tuple[str, ...]
    matched_queries: tuple[EpisodeRankedQuery, ...]


_CONTEXT_PART = re.compile(r"[\u3400-\u9fff]+|[a-z0-9][a-z0-9_.:+/-]*")


def _context_terms(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms: Counter[str] = Counter()
    for match in _CONTEXT_PART.finditer(normalized):
        part = match.group(0)
        if "\u3400" <= part[0] <= "\u9fff":
            if len(part) == 1:
                terms[part] += 1
                continue
            terms.update(part[index : index + 2] for index in range(len(part) - 1))
            if 2 < len(part) <= 4:
                terms[part] += 1
        elif len(part) >= 2:
            terms[part] += 1
    return terms


class _EpisodeContextIndex:
    def __init__(self, documents: list[EpisodeSearchDocument]) -> None:
        self._vectors: dict[str, Counter[str]] = {}
        frequencies: Counter[str] = Counter()
        for document in documents:
            vector: Counter[str] = Counter()
            for field in document.fields:
                multiplier = (
                    3
                    if field.name == "title"
                    else 2
                    if field.name in {"topic", "entity", "open_loop"}
                    else 1
                )
                for term, count in _context_terms(field.text).items():
                    vector[term] += multiplier * count
            for message in document.messages:
                vector.update(_context_terms(message.searchable_text))
            self._vectors[document.episode_id] = vector
            frequencies.update(vector.keys())
        document_count = len(documents)
        self._idf = {
            term: math.log1p(
                (document_count - frequency + 0.5) / (frequency + 0.5)
            )
            for term, frequency in frequencies.items()
        }
        self._norms = {
            episode_id: self._norm(vector)
            for episode_id, vector in self._vectors.items()
        }
        self._queries: dict[str, tuple[Counter[str], float]] = {}

    def _norm(self, vector: Counter[str]) -> float:
        return math.sqrt(
            sum(
                ((1.0 + math.log(count)) * self._idf.get(term, 0.0)) ** 2
                for term, count in vector.items()
            )
        )

    def similarity(self, context: str, episode_id: str) -> float:
        query, query_norm = self._query(context)
        document = self._vectors.get(episode_id)
        if not query or not document:
            return 0.0
        document_norm = self._norms.get(episode_id, 0.0)
        if query_norm <= 0 or document_norm <= 0:
            return 0.0
        common = query.keys() & document.keys()
        dot = sum(
            (1.0 + math.log(query[term]))
            * (1.0 + math.log(document[term]))
            * self._idf.get(term, 0.0) ** 2
            for term in common
        )
        return dot / (query_norm * document_norm)

    def coverage(self, context: str, episode_id: str) -> float:
        query, query_norm = self._query(context)
        document = self._vectors.get(episode_id)
        if not query or not document or query_norm <= 0:
            return 0.0
        covered = math.sqrt(
            sum(
                ((1.0 + math.log(count)) * self._idf.get(term, 0.0)) ** 2
                for term, count in query.items()
                if term in document
            )
        )
        return covered / query_norm

    def _query(self, context: str) -> tuple[Counter[str], float]:
        cached = self._queries.get(context)
        if cached is None:
            query = _context_terms(context)
            cached = (query, self._norm(query))
            self._queries[context] = cached
        return cached


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
    turn_support = 0.12 * min(
        math.log1p(max(0, hit.distinct_turn_count - 1)),
        math.log(4),
    )
    return strongest + corroboration + density + turn_support


def _priority_weight(priority: int) -> float:
    return _QUERY_PRIORITY_WEIGHTS[
        min(max(0, priority), len(_QUERY_PRIORITY_WEIGHTS) - 1)
    ]


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

    priority_weight = _priority_weight(query.priority)
    results: dict[str, tuple[float, list[EpisodeSearchHit]]] = {}
    for episode_id, scored_hits in by_episode.items():
        scored_hits.sort(key=lambda item: item[0], reverse=True)
        scores = [score for score, _ in scored_hits]
        combined = scores[0]
        if len(scores) > 1:
            combined += _SECOND_ALIAS_WEIGHT * scores[1]
        if len(scores) > 2:
            combined += _THIRD_ALIAS_WEIGHT * scores[2]
        results[episode_id] = (
            combined * priority_weight,
            [hit for _, hit in scored_hits],
        )
    return results


def _relevance_confidence(
    queries: list[EpisodeRecallQuery],
    matched_queries: tuple[EpisodeRankedQuery, ...],
    *,
    context_score: float,
    context_coverage: float,
) -> float:
    total_query_weight = sum(
        _priority_weight(query.priority) for query in queries
    )
    if total_query_weight <= 0:
        return 0.0
    matched_query_weight = sum(
        _priority_weight(query.priority) for query in matched_queries
    )
    query_coverage = matched_query_weight / total_query_weight
    alias_coverage = sum(
        _priority_weight(query.priority)
        * len(query.matched_alternatives)
        / max(1, query.alternative_count)
        for query in matched_queries
    ) / total_query_weight
    field_values = sorted(
        (
            min(1.0, _FIELD_WEIGHTS.get(field, 1.0) / 3.0)
            for query in matched_queries
            for field in query.field_matches
        ),
        reverse=True,
    )
    field_evidence = min(
        1.0,
        (field_values[0] if field_values else 0.0)
        + 0.12 * sum(field_values[1:3]),
    )
    turn_count = len(
        {turn_id for query in matched_queries for turn_id in query.turn_ids}
    )
    turn_support = min(1.0, math.log1p(turn_count) / math.log(4.0))
    context_alignment = math.sqrt(min(1.0, 2.0 * context_score))
    return (
        _CONFIDENCE_CONTEXT_COVERAGE_WEIGHT * context_coverage
        + _CONFIDENCE_CONTEXT_ALIGNMENT_WEIGHT * context_alignment
        + _CONFIDENCE_ALIAS_COVERAGE_WEIGHT * alias_coverage
        + _CONFIDENCE_TURN_SUPPORT_WEIGHT * turn_support
        + _CONFIDENCE_FIELD_EVIDENCE_WEIGHT * field_evidence
        + _CONFIDENCE_QUERY_COVERAGE_WEIGHT * query_coverage
    )


def rank_episode_matches(
    queries: list[EpisodeRecallQuery],
    matches: list[EpisodeQueryMatches],
    documents: list[EpisodeSearchDocument],
    *,
    limit: int = 8,
    offset: int = 0,
    now: float | None = None,
) -> list[RankedEpisodeHit]:
    if limit <= 0 or offset < 0 or not queries:
        return []
    if len(queries) != len(matches):
        raise ValueError("query and match counts differ")
    now = time.time() if now is None else now
    context_index = _EpisodeContextIndex(documents)
    episodes: dict[str, dict[str, object]] = {}
    for query_index, (query, query_matches) in enumerate(
        zip(queries, matches, strict=True)
    ):
        for episode_id, (score, hits) in _query_score(
            query,
            query_matches,
            len(documents),
        ).items():
            state = episodes.setdefault(
                episode_id,
                {
                    "queries": [],
                    "unit_scores": {},
                    "unit_context_scores": {},
                    "unit_context_coverage": {},
                    "matches": {},
                    "keywords": set(),
                    "last_activity_at": 0.0,
                    "salience": 0.0,
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
                expression=query.expression,
                unit_ids=query.unit_ids,
                priority=query.priority,
                score=score,
                matched_alternatives=alternatives,
                alternative_count=len(query_matches.alternatives),
                field_matches=tuple(sorted(field_matches)),
                message_ids=tuple(sorted(message_matches)),
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
                unit_scores.setdefault(unit_id, []).append(score)
                context = query.context.strip() or query.expression
                unit_context_scores = state["unit_context_scores"]
                assert isinstance(unit_context_scores, dict)
                unit_context_scores.setdefault(unit_id, []).append(
                    context_index.similarity(context, episode_id)
                )
                unit_context_coverage = state["unit_context_coverage"]
                assert isinstance(unit_context_coverage, dict)
                unit_context_coverage.setdefault(unit_id, []).append(
                    context_index.coverage(context, episode_id)
                )
            all_matches = state["matches"]
            assert isinstance(all_matches, dict)
            all_matches.update(message_matches)
            keywords = state["keywords"]
            assert isinstance(keywords, set)
            keywords.update(alternatives)
            state["last_activity_at"] = max(
                float(state["last_activity_at"]),
                max(hit.last_activity_at for hit in hits),
            )
            state["salience"] = max(
                float(state["salience"]),
                max(hit.salience for hit in hits),
            )

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
        unit_context_scores = state["unit_context_scores"]
        assert isinstance(unit_context_scores, dict)
        context_score = (
            sum(
                max(float(value) for value in scores)
                for scores in unit_context_scores.values()
            )
            / len(unit_context_scores)
            if unit_context_scores
            else 0.0
        )
        unit_context_coverage = state["unit_context_coverage"]
        assert isinstance(unit_context_coverage, dict)
        context_coverage = (
            sum(
                max(float(value) for value in scores)
                for scores in unit_context_coverage.values()
            )
            / len(unit_context_coverage)
            if unit_context_coverage
            else 0.0
        )
        semantic_score *= 1.0 + _CONTEXT_ALIGNMENT_WEIGHT * context_score
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
        relevance_confidence = _relevance_confidence(
            queries,
            ranked_queries,
            context_score=context_score,
            context_coverage=context_coverage,
        )
        keywords = state["keywords"]
        assert isinstance(keywords, set)
        ranked.append(
            RankedEpisodeHit(
                episode_id=episode_id,
                score=score,
                semantic_score=semantic_score,
                context_score=context_score,
                context_coverage=context_coverage,
                relevance_confidence=relevance_confidence,
                last_activity_at=last_activity_at,
                salience=salience,
                matches=ordered_matches,
                matched_keywords=tuple(sorted(str(value) for value in keywords)),
                matched_queries=ranked_queries,
            )
        )
    ranked.sort(
        key=lambda hit: (
            hit.score,
            hit.semantic_score,
            hit.last_activity_at,
            hit.episode_id,
        ),
        reverse=True,
    )
    relevant = [
        hit
        for hit in ranked
        if hit.relevance_confidence >= _RELEVANCE_CONFIDENCE_FLOOR
    ]
    return relevant[offset : offset + limit]


def recall_item_score(item: dict[str, object], *, now: float | None = None) -> float:
    relevance = float(
        item.get("recall_score") or item.get("search_score") or 0.0
    )
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
