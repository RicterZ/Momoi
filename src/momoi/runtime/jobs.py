from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


JobKind = Literal["goal", "reflection", "memory_maintenance", "heartbeat"]


@dataclass(frozen=True)
class AutonomousJob:
    kind: JobKind
    id: str = ""
    priority: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "priority",
            {"goal": 0, "reflection": 1, "memory_maintenance": 2, "heartbeat": 3}[
                self.kind
            ],
        )

    @classmethod
    def goal(cls, goal_id: str) -> "AutonomousJob":
        return cls("goal", goal_id)

    @classmethod
    def reflection(cls, local_date: str) -> "AutonomousJob":
        return cls("reflection", local_date)

    @classmethod
    def memory_maintenance(cls, turn_id: str) -> "AutonomousJob":
        return cls("memory_maintenance", turn_id)

    @classmethod
    def heartbeat(cls) -> "AutonomousJob":
        return cls("heartbeat")
