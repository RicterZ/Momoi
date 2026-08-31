import re
from html import unescape
from typing import Any

from momoi.models import ProviderResponse, ToolCall
from momoi.runtime.heartbeat_planner import HEARTBEAT_PLAN_TOOL_NAME
from momoi.runtime.protocol import RECALL_TOOL_SPEC
from momoi.runtime.turn_support import HEARTBEAT_PLANNER_SYSTEM_PROMPT


def planner_sections(text: str) -> dict[str, str]:
    return {
        match.group(1): unescape(match.group(2))
        for match in re.finditer(r"<([a-z_]+)>\n(.*?)\n</\1>", text, re.DOTALL)
    }


def recall_need(semantic: str, *keywords: str) -> dict[str, object]:
    return {"semantic": semantic, "keywords": list(keywords)}


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


def recall_response(units: int = 1) -> ProviderResponse:
    call = ToolCall(
        "submit-context",
        RECALL_TOOL_SPEC["name"],
        {
            "units": [
                {
                    "intent": "test owner intent",
                    "recall_mode": "search",
                    "recall_queries": [
                        {
                            "semantic": "Retrieve history for the test owner intent",
                            "keywords": ["test owner intent"],
                        }
                    ],
                    "recall_from_turn_id": "",
                    "episode": {"action": "none", "ref": "", "title": ""},
                }
                for _index in range(max(1, units))
            ]
        },
    )
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


class ContextAwareProvider:
    """Answer the Owner Turn's opening context decision, then delegate.

    Owner Turns must submit a recall decision before acting, so a fake provider
    that goes straight to send_message would spend its first rounds being
    refused. This keeps that protocol out of every individual test.
    """

    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    async def complete(
        self,
        system: object,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> ProviderResponse:
        names = {str(spec.get("name") or "") for spec in tools or []}
        last_recall = -1
        last_current_input = -1
        for index, message in enumerate(messages):
            content = (
                message.get("content")
                if isinstance(message.get("content"), list)
                else []
            )
            if any(
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == RECALL_TOOL_SPEC["name"]
                for block in content
            ):
                last_recall = index
            if any(
                isinstance(block, dict)
                and "<current_owner_messages>" in str(block.get("text") or "")
                for block in content
            ):
                last_current_input = index
        submitted = last_recall > last_current_input
        if RECALL_TOOL_SPEC["name"] in names and not submitted:
            return recall_response()
        return await self.delegate.complete(  # type: ignore[attr-defined,no-any-return]
            system, messages, tools, **kwargs
        )


class HeartbeatPlannerAwareProvider:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    async def complete(
        self,
        system: object,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> ProviderResponse:
        if system == HEARTBEAT_PLANNER_SYSTEM_PROMPT:
            return heartbeat_plan_response(messages)
        return await self.delegate.complete(  # type: ignore[attr-defined,no-any-return]
            system, messages, tools, **kwargs
        )


def with_owner_and_heartbeat_planner(provider: object) -> ContextAwareProvider:
    return ContextAwareProvider(HeartbeatPlannerAwareProvider(provider))
