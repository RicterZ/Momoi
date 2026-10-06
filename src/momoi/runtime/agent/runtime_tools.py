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
    tools: list[dict[str, Any]],
    tool_surface: ToolSurface,
) -> dict[str, object]:
    query = call.arguments.get("query")
    limit = call.arguments.get("limit", 5)
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 20:
        return {"ok": False, "error": "invalid_tool_search"}
    query = " ".join(query.casefold().split())
    terms = query.replace("_", " ").replace("-", " ").split()
    catalog = {spec["name"]: spec for specs in enable_tool_groups.values() for spec in specs}
    matches = []
    for name, spec in catalog.items():
        lower_name = name.casefold()
        description = str(spec.get("description") or "").casefold()
        score = (1000 if query == lower_name else 0)
        score += 300 if lower_name.startswith(query) else 0
        score += 200 if query in lower_name else 0
        score += 100 if query in description else 0
        score += sum((25 if term in lower_name else 0) + (10 if term in description else 0) for term in terms)
        if score:
            matches.append((score, name, spec))
    matches.sort(key=lambda item: (-item[0], item[1]))
    selected = [item[2] for item in matches[:limit]]
    enabled = tool_surface.append_visible(tools, selected)
    return {
        "ok": True, "state": "discovered", "query": query,
        "matched_tools": [spec["name"] for spec in selected], "newly_loaded_tools": enabled,
        **({"message": "未找到匹配工具，请换用工具名、前缀或其他关键词。"} if not selected else {}),
    }
