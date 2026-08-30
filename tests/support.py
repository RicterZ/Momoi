import re
from html import unescape
from typing import Any

from momoi.models import ProviderResponse, ToolCall
from momoi.runtime.context_planner import (
    CONTEXT_PLAN_TOOL_NAME,
    HEARTBEAT_PLAN_TOOL_NAME,
)
from momoi.runtime.turn_support import (
    CONTEXT_PLANNER_SYSTEM_PROMPT,
    HEARTBEAT_PLANNER_SYSTEM_PROMPT,
)


def planner_sections(text: str) -> dict[str, str]:
    return {
        match.group(1): unescape(match.group(2))
        for match in re.finditer(r"<([a-z_]+)>\n(.*?)\n</\1>", text, re.DOTALL)
    }


def recall_need(semantic: str, *keywords: str) -> dict[str, object]:
    return {"semantic": semantic, "keywords": list(keywords)}


def context_plan_response(messages: list[dict[str, Any]]) -> ProviderResponse:
    payload = planner_sections(str(messages[0]["content"]))
    owner_messages = [
        {
            "event_id": match.group(1),
            "text": match.group(2),
        }
        for match in re.finditer(
            r"\[event id=([^\s]+)[^]]*\]\n(.*?)(?=\n\n\[event |\Z)",
            payload["owner_messages"],
            re.DOTALL,
        )
    ]
    candidate_ids = re.findall(
        r"(?m)^- id=([^\s]+)", payload.get("candidate_episodes", "")
    )
    mcp_server_ids = re.findall(
        r"(?m)^- id=([^\s]+)", payload.get("available_mcp_servers", "")
    )
    units = [
        {
            "id": f"u{index}",
            "event_ids": [message["event_id"]],
            "text": message["text"],
            "intent": "test owner intent",
            "speech_act": "request",
            "references": [],
            "recall_mode": "search",
            "recall_queries": [recall_need("Retrieve history for the test owner intent", "test owner intent")],
            "recall_from_turn_id": "",
        }
        for index, message in enumerate(owner_messages, 1)
    ]
    episode_ref = candidate_ids[0] if candidate_ids else "new:test-thread"
    plan = {
        "version": 6,
        "intent_units": units,
        "episode_actions": [
            {
                "action": "continue" if candidate_ids else "new",
                "episode_ref": episode_ref,
                "unit_ids": [unit["id"] for unit in units],
                "topics": ["test"],
                "entities": [],
                "open_loops": [],
                "salience": 0.5,
                **({"title": "Test conversation"} if not candidate_ids else {}),
            }
        ],
        "episode_links": [],
        "handoff": {
            "context_needs": [],
            "mcp_servers": mcp_server_ids,
            "strategy": [
                "Handle the test owner request with the configured capabilities, "
                "verify the result, and report material uncertainty."
            ],
            "completion_criteria": ["The requested outcome is verified."],
            "response_mode": "visible",
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
    payload = planner_sections(str(messages[0]["content"]))
    plan = {
        "version": 3,
        "activity": {
            "intent": "spend time freely",
            "reason": "Continue the current activity for this test.",
            "recall_mode": "search",
            "recall_queries": [recall_need("History relevant to the current activity", "current activity")],
        },
        "heartbeat_handoff": {
            "context": {
                "status": "sufficient",
                "needs": [],
                "reason": "Test context is sufficient.",
            },
            "mcp": {
                "servers": re.findall(
                    r"(?m)^- id=([^\s]+)", payload.get("available_mcp_servers", "")
                ),
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
