"""Runtime product policies with behavior-preserving defaults.

Protocol/schema limits remain local domain invariants; these values describe
runtime product behavior and are intentionally immutable once injected.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DaemonPolicy:
    message_gap_min_seconds: float = 4.0
    message_gap_max_seconds: float = 7.0
    message_gap_min_chars: int = 4
    message_gap_saturation_chars: int = 60


@dataclass(frozen=True)
class ContextPolicy:
    max_visible_goals: int = 8


@dataclass(frozen=True)
class MemoryPolicy:
    recent_min_ttl_hours: float = 1.0
    recent_max_ttl_hours: float = 30 * 24


@dataclass(frozen=True)
class SemanticPolicy:
    query_failure_limit: int = 2
    query_breaker_seconds: float = 30.0
    candidate_floor: int = 32
    candidate_multiplier: int = 8
    active_poll_seconds: float = 0.05
    idle_poll_seconds: float = 2.0


@dataclass(frozen=True)
class RuntimePolicies:
    daemon: DaemonPolicy = DaemonPolicy()
    context: ContextPolicy = ContextPolicy()
    memory: MemoryPolicy = MemoryPolicy()
    semantic: SemanticPolicy = SemanticPolicy()
