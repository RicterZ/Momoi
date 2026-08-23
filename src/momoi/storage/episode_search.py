from dataclasses import dataclass
from typing import Protocol

from ..search import SearchBackend, search_alternatives


@dataclass(frozen=True)
class EpisodeSearchField:
    name: str
    text: str


@dataclass(frozen=True)
class EpisodeSearchMessage:
    id: int
    turn_id: str
    ordinal: int
    relation: str
    role: str
    content: str
    created_at: float
    delivery_state: str
    timestamp: str
    searchable_text: str
    scoped: bool = False


@dataclass(frozen=True)
class EpisodeSearchDocument:
    episode_id: str
    fields: tuple[EpisodeSearchField, ...]
    last_activity_at: float
    salience: float
    messages: tuple[EpisodeSearchMessage, ...]


@dataclass(frozen=True)
class EpisodeSearchHit:
    """Unranked evidence that one exact alternative matched one Episode."""

    episode_id: str
    alternative: str
    last_activity_at: float
    salience: float
    field_matches: tuple[str, ...]
    matches: tuple[EpisodeSearchMessage, ...]
    message_match_count: int
    distinct_turn_count: int


@dataclass(frozen=True)
class EpisodeAlternativeMatches:
    alternative: str
    hits: tuple[EpisodeSearchHit, ...]


@dataclass(frozen=True)
class EpisodeQueryMatches:
    expression: str
    alternatives: tuple[EpisodeAlternativeMatches, ...]


class EpisodeSearchBackend(Protocol):
    """Find evidence for one intact alternative without ranking Episodes."""

    def match_one(
        self,
        alternative: str,
        documents: list[EpisodeSearchDocument],
    ) -> list[EpisodeSearchHit]: ...


class StringEpisodeSearchBackend:
    """Deterministic exact-substring evidence collector."""

    def __init__(self, text_backend: SearchBackend) -> None:
        self.text_backend = text_backend

    def match_one(
        self,
        alternative: str,
        documents: list[EpisodeSearchDocument],
    ) -> list[EpisodeSearchHit]:
        hits: list[EpisodeSearchHit] = []
        for document in documents:
            field_matches = tuple(
                field.name
                for field in document.fields
                if self.text_backend.search_one(alternative, (field.text,))
                is not None
            )
            all_message_matches = tuple(
                message
                for message in document.messages
                if self.text_backend.search_one(
                    alternative,
                    (message.searchable_text,),
                )
                is not None
            )
            if not field_matches and not all_message_matches:
                continue
            ordered_matches = tuple(
                sorted(
                    all_message_matches,
                    key=lambda message: (message.ordinal, message.id),
                    reverse=True,
                )[:4]
            )
            hits.append(
                EpisodeSearchHit(
                    episode_id=document.episode_id,
                    alternative=alternative,
                    last_activity_at=document.last_activity_at,
                    salience=document.salience,
                    field_matches=field_matches,
                    matches=ordered_matches,
                    message_match_count=len(all_message_matches),
                    distinct_turn_count=len(
                        {message.turn_id for message in all_message_matches}
                    ),
                )
            )
        return hits


class EpisodeQueryService:
    """Collect evidence for query expressions; relevance belongs to the ranker."""

    def __init__(self, backend: EpisodeSearchBackend) -> None:
        self.backend = backend

    def match_many(
        self,
        expressions: list[str],
        documents: list[EpisodeSearchDocument],
    ) -> list[EpisodeQueryMatches]:
        parsed = [search_alternatives(expression) for expression in expressions]
        unique_alternatives = tuple(
            dict.fromkeys(
                alternative
                for alternatives in parsed
                for alternative in alternatives
            )
        )
        hits_by_alternative = {
            alternative: tuple(self.backend.match_one(alternative, documents))
            for alternative in unique_alternatives
        }
        return [
            EpisodeQueryMatches(
                expression=expression,
                alternatives=tuple(
                    EpisodeAlternativeMatches(
                        alternative=alternative,
                        hits=hits_by_alternative[alternative],
                    )
                    for alternative in alternatives
                ),
            )
            for expression, alternatives in zip(expressions, parsed, strict=True)
        ]

    def match(
        self,
        expression: str,
        documents: list[EpisodeSearchDocument],
    ) -> EpisodeQueryMatches:
        return self.match_many([expression], documents)[0]
