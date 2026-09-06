from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...models import ToolCall
from .harness import TURN_HARNESS_SPECS


class WorkflowProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class TurnExecutionSpec:
    """Non-combinable execution contract for one shared Agent loop."""

    stage: str
    goal_id: str | None = None
    allowed_capabilities: frozenset[str] | None = None
    artifact_root: Path | None = None
    permitted_tools: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.stage not in TURN_HARNESS_SPECS:
            raise ValueError(f"missing Turn execution spec for stage: {self.stage}")
        if (self.stage == "goal") != bool(self.goal_id):
            raise ValueError("goal execution requires exactly one goal_id")

    @property
    def authority(self) -> str:
        return self.stage if self.stage in {"owner", "webhook"} else "agent"

    @property
    def require_response(self) -> bool:
        return self.stage in {"owner", "heartbeat", "webhook", "reply_followup"}

    @property
    def accept_owner_updates(self) -> bool:
        return self.stage == "owner"

    @property
    def dynamic_tool_policies(self) -> bool:
        return self.stage in {
            "owner",
            "heartbeat",
            "webhook",
            "goal",
            "reply_followup",
        }

    @property
    def heartbeat(self) -> bool:
        return self.stage == "heartbeat"

    @property
    def reply_followup(self) -> bool:
        return self.stage == "reply_followup"


@dataclass(frozen=True)
class AgentWorkflow:
    """Business hooks for a private workflow hosted by the shared Agent loop."""

    stage: str
    tool_names: frozenset[str]
    execute_tool: Callable[[ToolCall], Awaitable[dict[str, Any]]]
    is_complete: Callable[[], bool]
    completion_result: Callable[[], dict[str, object] | None]
    no_tool_correction: str
