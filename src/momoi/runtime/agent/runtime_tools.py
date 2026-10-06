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
) -> dict[str, object]:
    if not heartbeat_turn or harness_started:
        return {"ok": False, "error": "invalid_heartbeat_begin"}
    return {
        "ok": True,
        "state": "started",
        "activity": call.arguments.get("activity"),
        "mode": call.arguments.get("mode"),
        "strategy": call.arguments.get("strategy"),
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
        "memory": recalled.get("memory_records", []),
        "reflection": recalled.get("reflection_records", []),
        **({"reflection_note": "复盘记忆可能过时，仅作辅助；以当前证据为准。"} if recalled.get("reflection_records") else {}),
        "episodes": recalled.get("episode_records", []),
    }


def search_tools(
    call: ToolCall,
    *,
    enable_tool_groups: dict[str, list[dict[str, Any]]],
    tool_surface: ToolSurface,
) -> dict[str, object]:
    query = call.arguments.get("query")
    limit = call.arguments.get("limit", 5)
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 20:
        return {"ok": False, "error": "invalid_tool_search"}
    query = " ".join(query.casefold().split())
    terms = query.replace("_", " ").replace("-", " ").split()
    matches = []
    for group, specs in enable_tool_groups.items():
        service_description = tool_surface.mcp_group_description(group).casefold()
        service_hit = query in group.casefold() or query in service_description
        for spec in specs:
            name = spec["name"]
            lower_name = name.casefold()
            description = str(spec.get("description") or "").casefold()
            score = 1000 if query == lower_name else 0
            score += 300 if lower_name.startswith(query) else 0
            score += 200 if query in lower_name else 0
            score += 100 if query in description else 0
            score += 50 if service_hit else 0
            score += sum((25 if term in lower_name else 0) + (10 if term in description else 0) for term in terms)
            if score:
                matches.append((score, name, spec))
    matches.sort(key=lambda item: (-item[0], item[1]))
    return {
        "ok": True,
        "tools": [{"name": spec["name"], "description": str(spec.get("description") or "")} for _, _, spec in matches[:limit]],
        "total": len(matches), "has_more": len(matches) > limit,
        **({"message": "未找到匹配工具，请换用服务名、工具名或其他关键词。"} if not matches else {}),
    }


def enable_tools(call: ToolCall, *, enable_tool_groups, tools, tool_surface):
    names = call.arguments.get("tools")
    if not isinstance(names, list) or not 1 <= len(names) <= 20 or any(not isinstance(name, str) or not name.strip() for name in names):
        return {"ok": False, "error": "invalid_tool_enable"}
    names = list(dict.fromkeys(names))
    catalog = {spec["name"]: spec for specs in enable_tool_groups.values() for spec in specs}
    unknown = [name for name in names if name not in catalog]
    if unknown:
        return {"ok": False, "error": "unknown_tools", "tools": unknown}
    newly_loaded = tool_surface.append_visible(tools, [catalog[name] for name in names])
    if tool_surface.store is not None:
        tool_surface.store.enable_transcript_tools(names)
    return {"ok": True, "enabled_tools": names, "newly_loaded_tools": newly_loaded}
