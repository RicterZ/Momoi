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
    max_visible_reminders: int = 8


@dataclass(frozen=True)
class MemoryPolicy:
    recent_min_ttl_hours: float = 1.0
    recent_max_ttl_hours: float = 7 * 24
    lexical_overlap_floor: float = 0.1


@dataclass(frozen=True)
class RuntimePolicies:
    daemon: DaemonPolicy = DaemonPolicy()
    context: ContextPolicy = ContextPolicy()
    memory: MemoryPolicy = MemoryPolicy()
