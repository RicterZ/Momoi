from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import ToolCall


class WorkflowProtocolError(RuntimeError):
    pass


END_TURN_STAGES = frozenset({"owner", "webhook", "heartbeat", "reply_followup"})


@dataclass(frozen=True)
class TurnExecutionSpec:
    """Non-combinable execution contract for one shared Agent loop."""

    stage: str
    goal_id: str | None = None
    allowed_capabilities: frozenset[str] | None = None
    artifact_root: Path | None = None

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
        return self.stage in END_TURN_STAGES

    @property
    def allow_notify(self) -> bool:
        return self.stage == "goal"

    @property
    def accept_owner_updates(self) -> bool:
        return self.stage == "owner"

    @property
    def dynamic_tool_policies(self) -> bool:
        return self.stage in {"owner", "heartbeat", "goal"}

    @property
    def heartbeat(self) -> bool:
        return self.stage == "heartbeat"

    @property
    def reply_followup(self) -> bool:
        return self.stage == "reply_followup"


@dataclass(frozen=True)
class TurnHarnessSpec:
    """Protocol-only state transitions for one kind of model Turn."""

    stage: str
    first_tool: str | None
    terminal_tool: str


TURN_HARNESS_SPECS = {
    spec.stage: spec
    for spec in (
        TurnHarnessSpec("owner", "recall", "end_turn"),
        TurnHarnessSpec("heartbeat", "heartbeat_begin", "end_turn"),
        TurnHarnessSpec("reply_followup", "send_bubbles", "end_turn"),
        TurnHarnessSpec("webhook", None, "end_turn"),
        TurnHarnessSpec("goal", None, "autonomous_finish"),
        TurnHarnessSpec("reflection", None, "reflection_finish"),
        TurnHarnessSpec(
            "memory_maintenance", None, "memory_maintenance_finish"
        ),
        TurnHarnessSpec(
            "episode_consolidate", None, "episode_consolidation_finish"
        ),
        TurnHarnessSpec("episode_anneal", None, "episode_summary_finish"),
    )
}


@dataclass
class TurnHarness:
    """Mutable protocol phase for a single Turn execution."""

    spec: TurnHarnessSpec
    started: bool = False

    def __post_init__(self) -> None:
        self.reset()

    @classmethod
    def for_stage(cls, stage: str) -> "TurnHarness":
        try:
            return cls(TURN_HARNESS_SPECS[stage])
        except KeyError as error:
            raise ValueError(f"missing Turn harness for stage: {stage}") from error

    def reset(self) -> None:
        self.started = self.spec.first_tool is None

    def validate_surface(self, tool_names: set[str]) -> None:
        required = {self.spec.terminal_tool}
        if self.spec.first_tool is not None:
            required.add(self.spec.first_tool)
        missing = required - tool_names
        if missing:
            raise ValueError(
                f"Turn harness {self.spec.stage} is missing tools: "
                + ", ".join(sorted(missing))
            )

    def validate(
        self, calls: list[ToolCall], *, has_assistant_text: bool = False
    ) -> str | None:
        if has_assistant_text:
            return "assistant_text_forbidden"
        names = [call.name for call in calls]
        first = self.spec.first_tool
        if first is not None and not self.started:
            if len(names) != 1 or names[0] != first:
                return f"{first}_must_be_first_and_alone"
        elif first is not None and first in names:
            return f"{first}_already_completed"
        terminal = self.spec.terminal_tool
        if terminal in names and (len(names) != 1 or names[0] != terminal):
            return f"{terminal}_must_be_alone"
        return None

    def accept(self, tool_name: str) -> None:
        if tool_name == self.spec.first_tool:
            self.started = True


@dataclass(frozen=True)
class AgentWorkflow:
    """Business hooks for a private workflow hosted by the shared Agent loop."""

    stage: str
    tool_names: frozenset[str]
    execute_tool: Callable[[ToolCall], Awaitable[dict[str, Any]]]
    is_complete: Callable[[], bool]
    completion_result: Callable[[], dict[str, object] | None]
    no_tool_correction: str
