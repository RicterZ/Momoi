from dataclasses import dataclass, field
from typing import Any

from .contracts import GoalMutation


def speaker_label(role: object) -> str:
    """Identity-neutral speaker label for archived conversation evidence."""

    value = str(role or "").strip().lower()
    return {"user": "OWNER", "assistant": "ASSISTANT"}.get(
        value, value.upper() or "UNKNOWN"
    )


@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    message_id: str
    text: str
    occurred_at: float
    received_at: float
    segments: tuple[dict[str, Any], ...] = ()
    channel: str = "unknown"
    delivery_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageRecalled:
    event_id: str
    message_id: str
    occurred_at: float
    channel: str
    author: str = "owner"


@dataclass(frozen=True)
class MessagePoked:
    event_id: str
    occurred_at: float
    channel: str
    author: str
    target: str


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
    # Opaque protocol continuation, separate from user-visible response content.
    continuation: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class TurnDraft:
    mood_update: dict[str, Any] | None = None
    heartbeat_activity: dict[str, Any] | None = None
    memory_operations: list[dict[str, Any]] = field(default_factory=list)
    memory_context: dict[int, dict[str, Any]] = field(default_factory=dict)
    memory_conversation: list[dict[str, Any]] = field(default_factory=list)
    goals: dict[str, GoalMutation] = field(default_factory=dict)
    notification_messages: list[str | dict[str, Any]] | None = None
    notification_key: str = ""
    notification_priority: str = "normal"
    notification_reason: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
