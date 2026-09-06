from dataclasses import dataclass

from ...models import ToolCall


@dataclass(frozen=True)
class TurnHarnessSpec:
    """Protocol-only state transitions for one kind of model Turn."""

    stage: str
    first_tool: str | None
    terminal_tool: str
    require_bubbles_before_progress_work: bool = False
    permitted_tools: frozenset[str] | None = None


TURN_HARNESS_SPECS = {
    spec.stage: spec
    for spec in (
        TurnHarnessSpec("owner", "recall", "end_turn", True),
        TurnHarnessSpec("heartbeat", "heartbeat_begin", "end_turn"),
        TurnHarnessSpec("reply_followup", "send_bubbles", "end_turn"),
        TurnHarnessSpec(
            "webhook",
            None,
            "end_turn",
            permitted_tools=frozenset(
                {"send_bubbles", "send_voice", "curl", "read_tool_result", "end_turn"}
            ),
        ),
        TurnHarnessSpec("goal", None, "end_turn"),
        TurnHarnessSpec("reflection", None, "reflection_finish"),
        TurnHarnessSpec("memory_maintenance", None, "memory_maintenance_finish"),
        TurnHarnessSpec("memory_operation", None, "memory_operation_finish", permitted_tools=frozenset({"memory_operation_finish", "memory_operation_search"})),
        TurnHarnessSpec("episode_consolidate", None, "episode_consolidation_finish"),
        TurnHarnessSpec("episode_anneal", None, "episode_summary_finish"),
    )
}


@dataclass
class TurnHarness:
    """Mutable protocol phase for a single Turn execution."""

    spec: TurnHarnessSpec
    progress_tool_names: frozenset[str] = frozenset()
    permitted_tool_names: frozenset[str] | None = None
    started: bool = False
    progress_bubbles_seen: bool = False
    blocked_tool_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        self.reset()

    @classmethod
    def for_stage(
        cls,
        stage: str,
        *,
        progress_tool_names: frozenset[str] = frozenset(),
        permitted_tool_names: frozenset[str] | None = None,
        blocked_tool_names: frozenset[str] = frozenset(),
    ) -> "TurnHarness":
        try:
            return cls(
                TURN_HARNESS_SPECS[stage],
                progress_tool_names=progress_tool_names,
                permitted_tool_names=permitted_tool_names,
                blocked_tool_names=blocked_tool_names,
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
        self,
        calls: list[ToolCall],
        *,
        has_assistant_text: bool = False,
        required_tool: str | None = None,
    ) -> str | None:
        if has_assistant_text:
            return "assistant_text_forbidden"
        names = [call.name for call in calls]
        if any(name in self.blocked_tool_names for name in names):
            return "tool_not_allowed"
        first = self.spec.first_tool
        first_names = {first}
        if (
            first == "send_bubbles"
            and (
                self.permitted_tool_names is None
                or "send_voice" in self.permitted_tool_names
            )
            and "send_voice" not in self.blocked_tool_names
        ):
            first_names.add("send_voice")
        if first is not None and not self.started:
            if len(names) != 1 or names[0] not in first_names:
                return f"{first}_must_be_first_and_alone"
        elif first is not None and any(name in first_names for name in names):
            return f"{first}_already_completed"
        if (
            required_tool is not None
            and required_tool != first
            and names != [required_tool]
        ):
            return f"{required_tool}_required"
        terminal = self.spec.terminal_tool
        if terminal in names and (len(names) != 1 or names[0] != terminal):
            return f"{terminal}_must_be_alone"
        permitted = (
            self.permitted_tool_names
            if self.permitted_tool_names is not None
            else self.spec.permitted_tools
        )
        if permitted is not None and any(name not in permitted for name in names):
            return "tool_not_allowed"
        for call in calls:
            if call.name != "end_turn":
                continue
            if call.argument_error or not isinstance(call.arguments, dict):
                return "invalid_end_turn_arguments"
            goal = call.arguments.get("goal")
            if self.spec.stage == "goal":
                if not isinstance(goal, dict):
                    return "goal_required_in_end_turn"
                if set(call.arguments) != {"goal"}:
                    return "goal_end_turn_only_accepts_goal"
            elif goal is not None:
                return "goal_not_allowed_in_end_turn"
        if (
            self.spec.require_bubbles_before_progress_work
            and not self.progress_bubbles_seen
        ):
            bubbles_seen = False
            for name in names:
                if name in {"send_bubbles", "send_voice"}:
                    bubbles_seen = True
                elif name in self.progress_tool_names and not bubbles_seen:
                    return "send_bubbles_required_before_progress_work"
        return None

    def observe_calls(self, calls: list[ToolCall]) -> None:
        """Record protocol-visible calls without interpreting tool results."""

        if self.spec.require_bubbles_before_progress_work and any(
            call.name in {"send_bubbles", "send_voice"} for call in calls
        ):
            self.progress_bubbles_seen = True

    def accept(self, tool_name: str) -> None:
        if tool_name == self.spec.first_tool or (
            self.spec.first_tool == "send_bubbles" and tool_name == "send_voice"
        ):
            self.started = True
