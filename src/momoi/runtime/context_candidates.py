from dataclasses import dataclass
import time

from ..storage import Store


@dataclass(frozen=True)
class EpisodeCandidatePolicy:
    search_limit: int = 8
    active_limit: int = 12
    directory_limit: int = 64
    total_limit: int = 64


DEFAULT_EPISODE_CANDIDATE_POLICY = EpisodeCandidatePolicy(
    search_limit=8,
    active_limit=2,
    directory_limit=8,
    total_limit=18,
)
_DEFAULT_EPISODE_LOOKBACK_SECONDS = 30 * 24 * 60 * 60


def collect_episode_candidates(
    store: Store,
    query: str,
    policy: EpisodeCandidatePolicy = DEFAULT_EPISODE_CANDIDATE_POLICY,
) -> list[dict[str, object]]:
    after = time.time() - _DEFAULT_EPISODE_LOOKBACK_SECONDS
    candidates: dict[str, dict[str, object]] = {}
    for candidate in [
        *store.search_episodes(query, policy.search_limit, after=after),
        *store.list_episode_candidates(policy.active_limit, after=after),
        *store.list_episode_directory(policy.directory_limit, after=after),
    ]:
        candidates.setdefault(str(candidate["id"]), candidate)
        if len(candidates) >= policy.total_limit:
            break
    return list(candidates.values())


def full_candidate_context(
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "id": candidate["id"],
            "status": candidate["status"],
            "title": candidate["title"],
            "created_timestamp": candidate.get("created_timestamp"),
            "updated_timestamp": candidate.get("updated_timestamp"),
            "summary": str(candidate["working_summary"] or candidate["summary"])[
                :400
            ],
            "topics": candidate["topics"],
            "entities": candidate["entities"],
            "open_loops": candidate["open_loops"],
        }
        for candidate in candidates
    ]
