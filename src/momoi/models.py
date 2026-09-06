from dataclasses import dataclass, field
from typing import Any

from .contracts import GoalMutation


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
class AgentReply:
    messages: list[str | dict[str, Any]]
    mood_update: dict[str, Any] | None = None
    heartbeat: dict[str, Any] | None = None
    reply_wait: dict[str, Any] | None = None
    activity_update: dict[str, Any] | None = None

    @property
    def should_schedule_reply_wait(self) -> bool:
        return bool(self.reply_wait and self.reply_wait.get("wait"))

    @property
    def expects_reply(self) -> bool:
        return self.should_schedule_reply_wait

    @property
    def reply_expectation(self) -> str:
        return (
            str(self.reply_wait.get("expected_information") or "")
            if self.should_schedule_reply_wait and self.reply_wait
            else ""
        )

    @property
    def reply_wait_delay_minutes(self) -> int:
        return (
            int(self.reply_wait.get("delay_minutes") or 0)
            if self.should_schedule_reply_wait and self.reply_wait
            else 0
        )

    @property
    def reply_wait_reason(self) -> str:
        return (
            str(self.reply_wait.get("reason") or "")
            if self.should_schedule_reply_wait and self.reply_wait
            else ""
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
    reasoning: str = ""


@dataclass
class TurnDraft:
    memory_operations: list[dict[str, Any]] = field(default_factory=list)
    memory_context: dict[int, dict[str, Any]] = field(default_factory=dict)
    memory_conversation: list[dict[str, Any]] = field(default_factory=list)
    goals: dict[str, GoalMutation] = field(default_factory=dict)
    notification_messages: list[str | dict[str, Any]] | None = None
    notification_key: str = ""
    notification_priority: str = "normal"
    notification_reason: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
