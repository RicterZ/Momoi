from dataclasses import dataclass
import math
import time
import unicodedata

from ..storage import Store


@dataclass(frozen=True)
class EpisodeCandidatePolicy:
    search_limit: int = 8
    active_limit: int = 12
    directory_limit: int = 64
    total_limit: int = 64


DEFAULT_EPISODE_CANDIDATE_POLICY = EpisodeCandidatePolicy(
    search_limit=32,
    active_limit=12,
    directory_limit=64,
    total_limit=18,
)
_DEFAULT_EPISODE_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
_FEATURE_WEIGHTS = {
    "exact_metadata": 0.28,
    "title_overlap": 0.22,
    "topics_overlap": 0.18,
    "entities_overlap": 0.14,
    "summary_overlap": 0.10,
    "open_loops_overlap": 0.18,
    "owner_message_overlap": 0.18,
    "assistant_message_overlap": 0.09,
    "recent_context": 0.38,
    "recent_open_loop": 0.22,
    "linked_context": 0.06,
    "recency": 0.04,
    "status": 0.02,
}
_GENERIC_METADATA = {
    "assistant",
    "momoi",
    "napcat",
    "owner",
    "user",
    "weixin",
    "主人",
    "小桃",
    "桃衣",
    "老师",
}


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _phrase_match(query: str, value: object) -> float:
    query_text = _normalized(query)
    value_text = _normalized(value)
    if not query_text or not value_text:
        return 0.0
    return float(query_text in value_text or value_text in query_text)


def _exact_metadata(query: str, candidate: dict[str, object]) -> float:
    normalized = query.casefold()
    values = [
        value
        for name in ("topics", "entities", "open_loops")
        for value in candidate.get(name, [])
        if isinstance(value, str)
    ]
    return float(
        any(
            len(value.strip()) >= 2
            and value.strip().casefold() not in _GENERIC_METADATA
            and value.strip().casefold() in normalized
            for value in values
        )
    )


def _message_overlap_by_role(
    query: str, candidate: dict[str, object], role: str
) -> float:
    return max(
        (
            _phrase_match(query, match.get("content"))
            for match in candidate.get("matches", [])
            if isinstance(match, dict) and str(match.get("role") or "") == role
        ),
        default=0.0,
    )


def _candidate_features(
    candidate: dict[str, object],
    query: str,
    context_scores: dict[str, dict[str, float]],
    now: float,
) -> dict[str, float]:
    episode_id = str(candidate["id"])
    last_activity = float(candidate.get("last_activity_at") or now)
    age_days = max(0.0, now - last_activity) / 86400
    status = str(candidate.get("status") or "")
    context = context_scores.get(episode_id, {})
    summary = (
        candidate.get("narrative_summary")
        or candidate.get("working_summary")
        or ""
    )
    return {
        "exact_metadata": _exact_metadata(query, candidate),
        "title_overlap": _phrase_match(query, candidate.get("title")),
        "topics_overlap": _phrase_match(query, candidate.get("topics")),
        "entities_overlap": _phrase_match(query, candidate.get("entities")),
        "summary_overlap": _phrase_match(query, summary),
        "open_loops_overlap": _phrase_match(query, candidate.get("open_loops")),
        "owner_message_overlap": _message_overlap_by_role(
            query, candidate, "user"
        ),
        "assistant_message_overlap": _message_overlap_by_role(
            query, candidate, "assistant"
        ),
        "recent_context": float(context.get("recent_context", 0.0)),
        "recent_open_loop": float(context.get("recent_context", 0.0))
        * float(bool(candidate.get("open_loops"))),
        "linked_context": float(context.get("linked_context", 0.0)),
        "recency": math.exp(-age_days / 7),
        "status": 1.0 if status == "open" else 0.5 if status == "closing" else 0.0,
    }


def rank_episode_candidates(
    candidates: list[dict[str, object]],
    query: str,
    context_scores: dict[str, dict[str, float]],
    limit: int,
    *,
    now: float | None = None,
) -> list[dict[str, object]]:
    now = time.time() if now is None else now
    ranked = []
    for candidate in candidates:
        features = _candidate_features(candidate, query, context_scores, now)
        contributions = {
            name: value * _FEATURE_WEIGHTS[name]
            for name, value in features.items()
        }
        score = sum(contributions.values())
        item = {
            **candidate,
            "match_score": round(score, 4),
            "match_features": {
                name: round(value, 4) for name, value in features.items()
            },
            "match_signals": [
                name
                for name, contribution in sorted(
                    contributions.items(), key=lambda pair: pair[1], reverse=True
                )
                if contribution > 0
            ][:4],
        }
        ranked.append(item)
    ranked.sort(
        key=lambda item: (
            float(item["match_score"]),
            float(item.get("last_activity_at") or 0),
            str(item["id"]),
        ),
        reverse=True,
    )
    return ranked[: max(0, limit)]


def collect_episode_candidates(
    store: Store,
    query: str,
    policy: EpisodeCandidatePolicy = DEFAULT_EPISODE_CANDIDATE_POLICY,
    *,
    recent_turn_ids: list[str] | None = None,
) -> list[dict[str, object]]:
    after = time.time() - _DEFAULT_EPISODE_LOOKBACK_SECONDS
    context_scores = store.episode_context_scores(recent_turn_ids or [])
    candidates: dict[str, dict[str, object]] = {}
    for candidate in [
        *store.search_episodes(query, policy.search_limit, after=after),
        *store.list_episode_candidates(policy.active_limit, after=after),
        *store.list_episode_directory(policy.directory_limit, after=after),
        *store.episode_candidates_by_ids(list(context_scores)),
    ]:
        candidates.setdefault(str(candidate["id"]), candidate)
    return rank_episode_candidates(
        list(candidates.values()),
        query,
        context_scores,
        policy.total_limit,
    )


def full_candidate_context(
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "id": candidate["id"],
            "status": candidate["status"],
            "title": candidate["title"],
            "created_timestamp": candidate.get("created_timestamp"),
            "last_activity_timestamp": candidate.get("last_activity_timestamp"),
            "summary": str(
                candidate.get("narrative_summary")
                or candidate.get("working_summary")
                or ""
            )[:400],
            "topics": candidate["topics"],
            "entities": candidate["entities"],
            "open_loops": candidate["open_loops"],
            "match_score": candidate.get("match_score", 0.0),
            "match_signals": candidate.get("match_signals", []),
        }
        for candidate in candidates
    ]
