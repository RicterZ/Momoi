from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...config.heartbeat_activities import HeartbeatActivity
from ...models import ToolCall
from .harness import TURN_HARNESS_SPECS


AUTHORITY_STAGES = frozenset({"owner", "webhook"})
RESPONSE_REQUIRED_STAGES = frozenset({"owner", "heartbeat", "webhook", "subagent_report"})
DYNAMIC_TOOL_POLICY_STAGES = frozenset({
    "owner", "heartbeat", "webhook", "subagent_report", "goal", "current_state_maintenance",
})
ASSISTANT_EXCHANGE_STAGES = frozenset({
    "owner", "heartbeat", "webhook", "subagent_report", "goal", "subagent",
})
CIRCUIT_BREAKER_STAGES = frozenset({"owner", "webhook", "subagent_report", "goal", "heartbeat"})


class WorkflowProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class TurnExecutionSpec:
    """Non-combinable execution contract for one shared Agent loop."""

    stage: str
    max_rounds: int = 0
    goal_id: str | None = None
    allowed_capabilities: frozenset[str] | None = None
    artifact_root: Path | None = None
    permitted_tools: frozenset[str] | None = None
    absorb_owner_updates: bool = True
    required_before_end: frozenset[str] = frozenset()
    heartbeat_activities: tuple[HeartbeatActivity, ...] = ()

    def __post_init__(self) -> None:
        if self.stage not in TURN_HARNESS_SPECS:
            raise ValueError(f"missing Turn execution spec for stage: {self.stage}")
        if (self.stage == "goal") != bool(self.goal_id):
            raise ValueError("goal execution requires exactly one goal_id")

    @property
    def authority(self) -> str:
        return self.stage if self.stage in AUTHORITY_STAGES else "agent"

    @property
    def require_response(self) -> bool:
        return self.stage in RESPONSE_REQUIRED_STAGES

    @property
    def accept_owner_updates(self) -> bool:
        return self.stage == "owner" and self.absorb_owner_updates

    @property
    def dynamic_tool_policies(self) -> bool:
        return self.stage in DYNAMIC_TOOL_POLICY_STAGES

    @property
    def heartbeat(self) -> bool:
        return self.stage == "heartbeat"


@dataclass(frozen=True)
class AgentWorkflow:
    """Business hooks for a private workflow hosted by the shared Agent loop."""

    stage: str
    tool_names: frozenset[str]
    execute_tool: Callable[[ToolCall], Awaitable[dict[str, Any]]]
    is_complete: Callable[[], bool]
    completion_result: Callable[[], dict[str, object] | None]
    no_tool_correction: str
    preserve_transcript: bool = False
    before_round: Callable[[int, list[dict[str, Any]]], Awaitable[None]] | None = None
    on_no_tools: Callable[[], None] | None = None
