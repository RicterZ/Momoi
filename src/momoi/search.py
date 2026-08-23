import unicodedata
from dataclasses import dataclass
from math import log
from typing import Iterable, Mapping, Protocol, TypeVar

NON_DISCRIMINATING_RATIO = 0.5
MIN_WEIGHTED_CORPUS = 10


def search_alternatives(query: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            normalized
            for part in query.replace("｜", "|").split("|")
            if (normalized := unicodedata.normalize("NFKC", part).casefold().strip())
        )
    )[:12]


@dataclass(frozen=True)
class SearchMatch:
    score: float
    alternatives: tuple[str, ...]
    alternative_scores: tuple[float, ...] = ()


class SearchBackend(Protocol):
    """Score one query phrase against a small in-memory text group."""

    def search_one(self, keyword: str, texts: Iterable[str]) -> float | None: ...


class StringSearchBackend:
    """Deterministic exact-substring baseline for small text groups."""

    def search_one(self, keyword: str, texts: Iterable[str]) -> float | None:
        needle = unicodedata.normalize("NFKC", keyword).casefold().strip()
        if not needle:
            return None
        return (
            1.0
            if any(
                needle in unicodedata.normalize("NFKC", text).casefold()
                for text in texts
                if text
            )
            else None
        )


DocumentT = TypeVar("DocumentT")
HitT = TypeVar("HitT")


class RankedSearchBackend(Protocol[DocumentT, HitT]):
    """Rank documents for one intact query alternative."""

    def search_one(
        self,
        keyword: str,
        documents: list[DocumentT],
        max_results: int,
    ) -> list[HitT]: ...


def document_frequency(
    alternatives: Iterable[str],
    corpus: Iterable[Iterable[str]],
    backend: SearchBackend,
) -> dict[str, int]:
    """Count how many documents of the corpus each alternative matches."""

    documents = tuple(tuple(document) for document in corpus)
    return {
        alternative: sum(
            1
            for document in documents
            if backend.search_one(alternative, document) is not None
        )
        for alternative in dict.fromkeys(alternatives)
    }


def alternative_weights(
    frequencies: Mapping[str, int],
    corpus_size: int,
) -> dict[str, float]:
    """Weigh each alternative by how much of the corpus it rules out.

    An alternative matching most of the corpus cannot separate the wanted
    records from the rest, so it is dropped instead of selecting the result set
    on its own. Weights stay within [0, 1] so callers keep the score
    calibration they already have.
    """

    if corpus_size < MIN_WEIGHTED_CORPUS:
        return {alternative: 1.0 for alternative in frequencies}
    ceiling = log(corpus_size)
    return {
        alternative: (
            0.0
            if not hits or hits > corpus_size * NON_DISCRIMINATING_RATIO
            else log(corpus_size / hits) / ceiling
        )
        for alternative, hits in frequencies.items()
    }


def discriminating_alternatives(
    alternatives: Iterable[str],
    weights: Mapping[str, float] | None,
) -> tuple[str, ...]:
    """Keep the alternatives that still narrow the corpus down."""

    if weights is None:
        return tuple(alternatives)
    return tuple(
        alternative
        for alternative in alternatives
        if weights.get(alternative, 1.0) > 0.0
    )


def search_expression(
    query: str,
    texts: Iterable[str],
    backend: SearchBackend,
    *,
    weights: Mapping[str, float] | None = None,
) -> SearchMatch | None:
    """Evaluate an OR expression while keeping each alternative intact."""

    alternatives = discriminating_alternatives(search_alternatives(query), weights)
    if not alternatives:
        return None
    materialized = tuple(texts)
    scored = tuple(
        (alternative, score)
        for alternative in alternatives
        if (score := backend.search_one(alternative, materialized)) is not None
    )
    if not scored:
        return None
    return SearchMatch(
        sum((weights or {}).get(alternative, 1.0) for alternative, _ in scored)
        / len(alternatives),
        tuple(alternative for alternative, _ in scored),
        tuple(float(score) for _, score in scored),
    )
