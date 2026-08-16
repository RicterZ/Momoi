import unicodedata
from dataclasses import dataclass
from typing import Iterable, Protocol


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


class SearchBackend(Protocol):
    def search_one(self, keyword: str, texts: Iterable[str]) -> float | None: ...


class StringSearchBackend:
    """Exact substring search. Replace this backend with vector search later."""

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
    matched = tuple(
        alternative
        for alternative in alternatives
        if backend.search_one(alternative, materialized) is not None
    )
    if not matched:
        return None
    return SearchMatch(len(matched) / len(alternatives), matched)
