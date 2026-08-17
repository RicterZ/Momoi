from dataclasses import dataclass, field
from typing import Any

from .contracts import GoalMutation, ReminderMutation


@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    message_id: str
    text: str
    occurred_at: float
    received_at: float
    segments: tuple[dict[str, Any], ...] = ()
    channel: str = "unknown"


@dataclass(frozen=True)
class OwnerInputStatus:
    channel: str


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    turn_id: str
    text: str
    state: str
    attempts: int
    kind: str = "text"
    media_path: str | None = None
    payload: dict[str, Any] | None = None
    channel: str = ""


@dataclass(frozen=True)
class MemoryCandidate:
    kind: str
    key: str
    content: str
    evidence: str
    importance: float = 0.5
    replace_confirmed: bool = False
    activation: str = "recall"
    ttl_hours: float = 0


@dataclass(frozen=True)
class MemoryForgetCandidate:
    kind: str
    key: str
    evidence: str


@dataclass(frozen=True)
class MemoryConflictCandidate:
    kind: str
    key: str
    content: str
    evidence: str
    importance: float = 0.5
    activation: str = "recall"


@dataclass(frozen=True)
class AgentReply:
    messages: list[str | dict[str, Any]]
    mood_update: dict[str, Any] | None = None
    expects_reply: bool = False
    reply_expectation: str = ""
    schedule_reply_wait: bool | None = None
    heartbeat: dict[str, Any] | None = None
    reply_wait: dict[str, Any] | None = None

    @property
    def should_schedule_reply_wait(self) -> bool:
        return (
            self.expects_reply
            if self.schedule_reply_wait is None
            else self.schedule_reply_wait
        )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    argument_error: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    content: list[dict[str, Any]]
    tool_calls: list[ToolCall]
    usage: dict[str, float | int | bool] | None = None


@dataclass
class TurnDraft:
    memories: list[MemoryCandidate] = field(default_factory=list)
    memory_conflicts: list[MemoryConflictCandidate] = field(default_factory=list)
    forgotten_memories: list[MemoryForgetCandidate] = field(default_factory=list)
    goals: dict[str, GoalMutation] = field(default_factory=dict)
    reminders: dict[str, ReminderMutation] = field(default_factory=dict)
    notification_messages: list[str] | None = None
    notification_key: str = ""
    notification_priority: str = "normal"
    notification_reason: str = ""
    close_reply_expectation: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
