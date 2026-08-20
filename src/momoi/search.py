import unicodedata
from dataclasses import dataclass
from typing import Iterable, Protocol, TypeVar


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


def search_expression(
    query: str,
    texts: Iterable[str],
    backend: SearchBackend,
) -> SearchMatch | None:
    """Evaluate an OR expression while keeping each alternative intact."""

    alternatives = search_alternatives(query)
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
        len(scored) / len(alternatives),
        tuple(alternative for alternative, _ in scored),
        tuple(float(score) for _, score in scored),
    )
