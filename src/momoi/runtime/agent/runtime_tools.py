from collections.abc import Awaitable, Callable
from typing import Any

from ...models import IncomingMessage, ToolCall, TurnDraft
from .tool_surface import ToolSurface
from ..tool_contracts.context import recall_correction


def record_heartbeat_activity(
    call: ToolCall,
    *,
    heartbeat_turn: bool,
    draft: TurnDraft,
    minimum_seconds: int = 60,
    maximum_seconds: int = 86400,
) -> dict[str, object]:
    if not heartbeat_turn:
        return {"ok": False, "error": "tool_not_allowed"}
    args = call.arguments
    if (
        not isinstance(args, dict)
        or set(args) != {"activity", "result", "next_check_minutes", "reason"}
        or not isinstance(args["activity"], str)
        or not args["activity"].strip()
        or len(args["activity"]) > 300
        or not isinstance(args["result"], str)
        or len(args["result"]) > 2000
    ):
        return {"ok": False, "error": "invalid_heartbeat_activity"}
    if (
        type(args["next_check_minutes"]) is not int
        or not 1 <= args["next_check_minutes"] <= 1440
        or not minimum_seconds <= args["next_check_minutes"] * 60 <= maximum_seconds
        or not isinstance(args["reason"], str)
        or not args["reason"].strip()
        or len(args["reason"]) > 500
    ):
        return {
            "ok": False,
            "error": "invalid_heartbeat_schedule",
            "message": f"Supply next_check_minutes within {minimum_seconds}-{maximum_seconds} seconds and a nonempty reason (at most 500 characters).",
        }
    draft.heartbeat_activity = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in args.items()
    }
    return {"ok": True, "state": "staged"}


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
    selected_tools = tool_surface.append_visible(
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
        "enabled_tools": selected_tools,
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
        return {"ok": False, "error": "invalid_recall", **recall_correction(str(error))}
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
    groups = list(dict.fromkeys(requested))
    enabled = tool_surface.append_visible(
        tools,
        [spec for group in groups for spec in enable_tool_groups[group]],
    )
    return {"ok": True, "state": "enabled", "groups": groups, "tools": enabled}
