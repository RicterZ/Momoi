import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from ..search import RankedSearchBackend, SearchBackend, search_alternatives


@dataclass(frozen=True)
class EpisodeSearchMessage:
    id: int
    turn_id: str
    ordinal: int
    role: str
    content: str
    created_at: float
    delivery_state: str
    timestamp: str
    searchable_text: str


@dataclass(frozen=True)
class EpisodeSearchDocument:
    episode_id: str
    metadata: tuple[str, ...]
    last_activity_at: float
    salience: float
    messages: tuple[EpisodeSearchMessage, ...]

    @property
    def search_id(self) -> str:
        return self.episode_id

    @property
    def search_revision(self) -> str:
        payload = {
            "metadata": self.metadata,
            "last_activity_at": self.last_activity_at,
            "salience": self.salience,
            "messages": [
                {
                    "id": message.id,
                    "turn_id": message.turn_id,
                    "ordinal": message.ordinal,
                    "role": message.role,
                    "delivery_state": message.delivery_state,
                    "searchable_text": message.searchable_text,
                }
                for message in self.messages
            ],
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EpisodeSearchHit:
    episode_id: str
    score: float
    last_activity_at: float
    matches: tuple[EpisodeSearchMessage, ...]
    matched_keywords: tuple[str, ...] = ()


class EpisodeSearchBackend(
    RankedSearchBackend[EpisodeSearchDocument, EpisodeSearchHit],
    Protocol,
):
    """Episode-specific specialization of the ranked document backend."""


class StringEpisodeSearchBackend:
    """Exact-string ranked document baseline."""

    def __init__(self, text_backend: SearchBackend) -> None:
        self.text_backend = text_backend

    def search_one(
        self,
        keyword: str,
        documents: list[EpisodeSearchDocument],
        max_results: int,
    ) -> list[EpisodeSearchHit]:
        ranked: list[
            tuple[float, int, float, float, str, tuple[EpisodeSearchMessage, ...]]
        ] = []
        for document in documents:
            metadata_match = self.text_backend.search_one(
                keyword, document.metadata
            )
            matches = tuple(
                sorted(
                    (
                        message
                        for message in document.messages
                        if self.text_backend.search_one(
                            keyword, (message.searchable_text,)
                        )
                        is not None
                    ),
                    key=lambda message: (message.ordinal, message.id),
                    reverse=True,
                )[:4]
            )
            if metadata_match is None and not matches:
                continue
            score = max(metadata_match or 0.0, float(bool(matches)))
            ranked.append(
                (
                    score,
                    len(matches),
                    document.last_activity_at,
                    document.salience,
                    document.episode_id,
                    matches,
                )
            )
        ranked.sort(key=lambda item: item[:5], reverse=True)
        return [
            EpisodeSearchHit(
                episode_id,
                score,
                last_activity_at,
                matches,
                (keyword,),
            )
            for score, _, last_activity_at, _, episode_id, matches in ranked[
                :max_results
            ]
        ]


class EpisodeQueryService:
    """Parse `A | B | C`, search each phrase, then merge and rank Episodes."""

    def __init__(self, backend: EpisodeSearchBackend) -> None:
        self.backend = backend

    def search(
        self,
        expression: str,
        documents: list[EpisodeSearchDocument],
        max_results: int,
        *,
        offset: int = 0,
    ) -> list[EpisodeSearchHit]:
        alternatives = search_alternatives(expression)
        if not alternatives or max_results <= 0 or offset < 0:
            return []
        merged: dict[
            str,
            dict[str, object],
        ] = {}
        per_alternative_limit = max(max_results, len(documents))
        for alternative in alternatives:
            for rank, hit in enumerate(
                self.backend.search_one(
                    alternative, documents, per_alternative_limit
                )
            ):
                state = merged.setdefault(
                    hit.episode_id,
                    {
                        "alternatives": set(),
                        "reciprocal_rank": 0.0,
                        "score": 0.0,
                        "last_activity_at": hit.last_activity_at,
                        "matches": {},
                    },
                )
                alternatives_seen = state["alternatives"]
                assert isinstance(alternatives_seen, set)
                alternatives_seen.add(alternative)
                state["reciprocal_rank"] = float(state["reciprocal_rank"]) + (
                    1.0 / (rank + 1)
                )
                state["score"] = max(float(state["score"]), hit.score)
                matches = state["matches"]
                assert isinstance(matches, dict)
                for message in hit.matches:
                    matches.setdefault(message.id, message)
        ranked = sorted(
            merged.items(),
            key=lambda item: (
                len(item[1]["alternatives"]),
                float(item[1]["reciprocal_rank"]),
                float(item[1]["score"]),
                float(item[1]["last_activity_at"]),
                item[0],
            ),
            reverse=True,
        )
        results: list[EpisodeSearchHit] = []
        for episode_id, state in ranked[offset : offset + max_results]:
            matches = state["matches"]
            assert isinstance(matches, dict)
            ordered_matches = tuple(
                sorted(
                    matches.values(),
                    key=lambda message: (message.ordinal, message.id),
                    reverse=True,
                )[:4]
            )
            results.append(
                EpisodeSearchHit(
                    episode_id=episode_id,
                    score=float(state["score"]),
                    last_activity_at=float(state["last_activity_at"]),
                    matches=ordered_matches,
                    matched_keywords=tuple(
                        sorted(str(value) for value in state["alternatives"])
                    ),
                )
            )
        return results
