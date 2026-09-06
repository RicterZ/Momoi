from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal


JobKind = Literal["goal", "reflection", "memory_maintenance", "memory_operation", "heartbeat"]


@dataclass(frozen=True)
class AutonomousJob:
    kind: JobKind
    id: str = ""
    priority: int = field(init=False)
    wait_rounds: int = field(default=0, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "priority",
            {"goal": 0, "memory_operation": 1, "reflection": 1, "memory_maintenance": 2, "heartbeat": 3}[
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
    def memory_operation(cls, batch_id: str) -> "AutonomousJob":
        return cls("memory_operation", batch_id)

    @classmethod
    def heartbeat(cls) -> "AutonomousJob":
        return cls("heartbeat")

    @property
    def effective_priority(self) -> int:
        return self.priority - self.wait_rounds

    def waited(self) -> "AutonomousJob":
        return replace(self, wait_rounds=self.wait_rounds + 1)
