import json
from typing import Any

from momoi.models import ProviderResponse, ToolCall
from momoi.runtime.context_planner import (
    CONTEXT_PLAN_TOOL_NAME,
    HEARTBEAT_PLAN_TOOL_NAME,
)
from momoi.runtime.turns import (
    CONTEXT_PLANNER_SYSTEM_PROMPT,
    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
)


def context_plan_response(messages: list[dict[str, Any]]) -> ProviderResponse:
    payload = json.loads(str(messages[0]["content"]))
    owner_messages = payload["owner_messages"]
    candidates = payload["candidate_episodes"]
    units = [
        {
            "id": f"u{index}",
            "event_ids": [message["event_id"]],
            "text": message["text"],
            "intent": "test owner intent",
            "speech_act": "request",
            "references": [],
        }
        for index, message in enumerate(owner_messages, 1)
    ]
    episode_ref = candidates[0]["id"] if candidates else "new:test-thread"
    plan = {
        "version": 2,
        "intent_units": units,
        "episode_actions": [
            {
                "action": "continue" if candidates else "new",
                "episode_ref": episode_ref,
                "unit_ids": [unit["id"] for unit in units],
                "topics": ["test"],
                "entities": [],
                "open_loops": [],
                "salience": 0.5,
                **({"title": "Test conversation"} if not candidates else {}),
            }
        ],
        "episode_links": [],
        "owner_handoff": {
            "context": {
                "status": "sufficient",
                "needs": [],
                "reason": "Test context is sufficient.",
            },
            "mcp": {
                "servers": [
                    str(server["id"])
                    for server in payload.get("available_mcp_servers", [])
                ],
                "reason": "Load configured test MCP servers.",
            },
            "execution": {
                "mode": "work",
                "outline": ["Handle the test owner request."],
                "reason": "Exercise the Owner tool loop.",
            },
        },
        "uncertainty": [],
    }
    call = ToolCall("context-plan", CONTEXT_PLAN_TOOL_NAME, plan)
    return ProviderResponse(
        [
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        ],
        [call],
    )


def heartbeat_plan_response(messages: list[dict[str, Any]]) -> ProviderResponse:
    payload = json.loads(str(messages[0]["content"]))
    previous = payload["previous_activity"]
    plan = {
        "version": 2,
        "activity": {
            "intent": str(previous.get("activity") or "spend time freely"),
            "reason": "Continue the current activity for this test.",
        },
        "heartbeat_handoff": {
            "context": {
                "status": "sufficient",
                "needs": [],
                "reason": "Test context is sufficient.",
            },
            "mcp": {
                "servers": [
                    str(server["id"])
                    for server in payload.get("available_mcp_servers", [])
                ],
                "reason": "Load configured test MCP servers.",
            },
            "execution": {
                "mode": "work",
                "outline": ["Continue the selected test activity."],
                "reason": "Exercise the Heartbeat Turn.",
            },
        },
        "uncertainty": [],
    }
    call = ToolCall("heartbeat-plan", HEARTBEAT_PLAN_TOOL_NAME, plan)
    return ProviderResponse(
        [
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        ],
        [call],
    )


class PlannerAwareProvider:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    async def complete(
        self,
        system: object,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> ProviderResponse:
        if system == CONTEXT_PLANNER_SYSTEM_PROMPT:
            return context_plan_response(messages)
        if system == HEARTBEAT_PLANNER_SYSTEM_PROMPT:
            return heartbeat_plan_response(messages)
        return await self.delegate.complete(  # type: ignore[attr-defined,no-any-return]
            system, messages, tools, **kwargs
        )


def with_context_planner(provider: object) -> PlannerAwareProvider:
    return PlannerAwareProvider(provider)
