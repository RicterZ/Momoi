"""Retrieval values; no database, provider, or runtime dependencies."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryRecallQuery:
    expression: str
    unit_ids: tuple[str, ...] = ()
    priority: int = 0
    semantic_expression: str = ""
    # Empty means all canonical memory kinds.  This is deliberately part of
    # the query (rather than a post-filter) so sparse and dense ranking share
    # the same eligibility boundary.
    kinds: tuple[str, ...] = ()

    @property
    def dense_expression(self) -> str:
        return self.semantic_expression.strip() or self.expression.strip()


@dataclass(frozen=True)
class DenseThresholds:
    support: float
    only: float
    strong: float

    def calibrated(self, cosine: float) -> float:
        if self.strong <= self.support:
            return 0.0
        return min(
            1.0, max(0.0, (cosine - self.support) / (self.strong - self.support))
        )


@dataclass(frozen=True)
class DenseMemoryHit:
    source_id: str
    cosine: float


@dataclass(frozen=True)
class DenseEpisodeHit:
    episode_id: str
    summary_cosine: float | None = None
    turn_cosine: float | None = None
    cue_cosine: float | None = None

    @property
    def cosine(self) -> float:
        return max(
            value
            for value in (self.summary_cosine, self.turn_cosine, self.cue_cosine)
            if value is not None
        )
