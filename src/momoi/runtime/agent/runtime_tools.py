from collections.abc import Awaitable, Callable
from typing import Any

from ...models import IncomingMessage, ToolCall
from .tool_surface import ToolSurface


async def begin_heartbeat(
    call: ToolCall,
    *,
    heartbeat_turn: bool,
    harness_started: bool,
    enable_tool_groups: dict[str, list[dict[str, Any]]],
    tools: list[dict[str, Any]],
    tool_surface: ToolSurface,
    prepare_context: Callable[[dict[str, Any]], Awaitable[dict[str, object]]],
) -> dict[str, object]:
    requested = call.arguments.get("tool_groups")
    if (
        not heartbeat_turn
        or harness_started
        or not isinstance(requested, list)
        or any(
            not isinstance(group, str) or group not in enable_tool_groups
            for group in requested
        )
    ):
        return {"ok": False, "error": "invalid_heartbeat_begin"}
    try:
        prepared = await prepare_context(call.arguments)
    except ValueError as error:
        return {
            "ok": False,
            "error": "invalid_heartbeat_begin",
            "message": str(error),
        }
    enabled_tools = tool_surface.append_visible(
        tools,
        [
            spec
            for group in dict.fromkeys(requested)
            for spec in enable_tool_groups[group]
        ],
    )
    recalled = prepared["context"]
    assert isinstance(recalled, dict)
    return {
        "ok": True,
        "state": "started",
        "activity": call.arguments.get("activity"),
        "mode": call.arguments.get("mode"),
        "strategy": call.arguments.get("strategy"),
        "memory": recalled["recall_memories"],
        "status": recalled["query_recall"],
        "reflection": recalled["reflection_memories"],
        "episodes": recalled["episodes"],
        "enabled_tools": enabled_tools,
    }


async def recall_owner_context(
    call: ToolCall,
    *,
    current_events: list[IncomingMessage],
    turn_id: str,
    submit_context: Callable[
        [list[IncomingMessage], str, dict[str, Any]],
        Awaitable[dict[str, object]],
    ],
) -> dict[str, object]:
    try:
        recalled = await submit_context(current_events, turn_id, call.arguments)
    except ValueError as error:
        return {"ok": False, "error": "invalid_recall", "message": str(error)}
    return {
        "ok": True,
        "state": "recalled",
        "memory": recalled["recall_memories"],
        "status": recalled["query_recall"],
        "reflection": recalled["reflection_memories"],
        "episodes": recalled["episodes"],
    }


def enable_tools(
    call: ToolCall,
    *,
    enable_tool_groups: dict[str, list[dict[str, Any]]],
    tools: list[dict[str, Any]],
    tool_surface: ToolSurface,
) -> dict[str, object]:
    requested = call.arguments.get("groups")
    if (
        not isinstance(requested, list)
        or not requested
        or any(
            not isinstance(group, str) or group not in enable_tool_groups
            for group in requested
        )
    ):
        return {"ok": False, "error": "invalid_tool_groups"}
    ordered_groups = list(dict.fromkeys(requested))
    enabled_tools = tool_surface.append_visible(
        tools,
        [spec for group in ordered_groups for spec in enable_tool_groups[group]],
    )
    return {
        "ok": True,
        "state": "enabled",
        "groups": ordered_groups,
        "tools": enabled_tools,
    }
