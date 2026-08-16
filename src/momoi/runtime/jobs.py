from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


JobKind = Literal["goal", "reflection", "heartbeat"]

HEARTBEAT_QUEUE_ITEM = "__momoi_heartbeat__"
REFLECTION_QUEUE_PREFIX = "__momoi_reflection__:"


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

    @classmethod
    def from_legacy(cls, value: str | AutonomousJob) -> AutonomousJob:
        if isinstance(value, cls):
            return value
        if value == HEARTBEAT_QUEUE_ITEM:
            return cls.heartbeat()
        if value.startswith(REFLECTION_QUEUE_PREFIX):
            return cls.reflection(value.removeprefix(REFLECTION_QUEUE_PREFIX))
        return cls.goal(value)

    def legacy_value(self) -> str:
        if self.kind == "heartbeat":
            return HEARTBEAT_QUEUE_ITEM
        if self.kind == "reflection":
            return REFLECTION_QUEUE_PREFIX + self.id
        return self.id
