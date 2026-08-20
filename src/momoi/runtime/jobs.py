from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


JobKind = Literal["goal", "reflection", "heartbeat"]


@dataclass(frozen=True)
class AutonomousJob:
    kind: JobKind
    id: str = ""
    priority: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "priority", {"goal": 0, "reflection": 1, "heartbeat": 2}[self.kind]
        )

    @classmethod
    def goal(cls, goal_id: str) -> "AutonomousJob":
        return cls("goal", goal_id)

    @classmethod
    def reflection(cls, local_date: str) -> "AutonomousJob":
        return cls("reflection", local_date)

    @classmethod
    def heartbeat(cls) -> "AutonomousJob":
        return cls("heartbeat")
