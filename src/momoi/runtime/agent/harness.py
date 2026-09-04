from dataclasses import dataclass

from ...models import ToolCall


@dataclass(frozen=True)
class TurnHarnessSpec:
    """Protocol-only state transitions for one kind of model Turn."""

    stage: str
    first_tool: str | None
    terminal_tool: str
    require_bubbles_before_progress_work: bool = False


TURN_HARNESS_SPECS = {
    spec.stage: spec
    for spec in (
        TurnHarnessSpec("owner", "recall", "end_turn", True),
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
    progress_tool_names: frozenset[str] = frozenset()
    started: bool = False
    progress_bubbles_seen: bool = False

    def __post_init__(self) -> None:
        self.reset()

    @classmethod
    def for_stage(
        cls,
        stage: str,
        *,
        progress_tool_names: frozenset[str] = frozenset(),
    ) -> "TurnHarness":
        try:
            return cls(
                TURN_HARNESS_SPECS[stage],
                progress_tool_names=progress_tool_names,
            )
        except KeyError as error:
            raise ValueError(f"missing Turn harness for stage: {stage}") from error

    def reset(self) -> None:
        self.started = self.spec.first_tool is None
        self.progress_bubbles_seen = False

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
        if (
            self.spec.require_bubbles_before_progress_work
            and not self.progress_bubbles_seen
        ):
            bubbles_seen = False
            for name in names:
                if name == "send_bubbles":
                    bubbles_seen = True
                elif name in self.progress_tool_names and not bubbles_seen:
                    return "send_bubbles_required_before_progress_work"
        return None

    def observe_calls(self, calls: list[ToolCall]) -> None:
        """Record protocol-visible calls without interpreting tool results."""

        if self.spec.require_bubbles_before_progress_work and any(
            call.name == "send_bubbles" for call in calls
        ):
            self.progress_bubbles_seen = True

    def accept(self, tool_name: str) -> None:
        if tool_name == self.spec.first_tool:
            self.started = True
