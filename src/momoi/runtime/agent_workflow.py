from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..models import ToolCall


class WorkflowProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentWorkflow:
    """Business hooks for a private workflow hosted by the shared Agent loop."""

    stage: str
    tool_names: frozenset[str]
    execute_tool: Callable[[ToolCall], Awaitable[dict[str, Any]]]
    is_complete: Callable[[], bool]
    completion_result: Callable[[], dict[str, object] | None]
    no_tool_correction: str
