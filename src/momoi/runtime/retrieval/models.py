from dataclasses import dataclass, field

from ...memory.retrieval.models import DenseThresholds, DenseMemoryHit
from ...storage.episode.episode_ranking import DenseEpisodeHit


CALIBRATION_PROFILES: dict[str, dict[str, tuple[float, float, float]]] = {
    # Calibrated against the private historical benchmark for this model. The
    # episode-only gate is intentionally stricter: generic episode summaries
    # otherwise produce high cosine scores without sparse/topic corroboration.
    "bge-small-zh-v1.5-momoi-v1": {
        "confirmed_memory": (0.55, 0.72, 0.86),
        "reflection_memory": (0.58, 0.75, 0.87),
        "episode_summary": (0.52, 0.81, 0.84),
        "episode_turn": (0.56, 0.81, 0.86),
        "episode_cue": (0.52, 0.72, 0.84),
    }
}


@dataclass(frozen=True)
class DenseRecallEvidence:
    space_id: str = ""
    calibration_profile: str = ""
    memory: dict[str, dict[tuple[str, str], DenseMemoryHit]] = field(
        default_factory=dict
    )
    episodes: dict[str, dict[str, DenseEpisodeHit]] = field(default_factory=dict)
    query_batch_size: int = 0
    request_ms: float = 0.0
    search_ms: float = 0.0
    fallback_reason: str = ""

    def thresholds(self, document_type: str) -> DenseThresholds | None:
        values = CALIBRATION_PROFILES.get(self.calibration_profile, {}).get(
            document_type
        )
        return DenseThresholds(*values) if values else None
