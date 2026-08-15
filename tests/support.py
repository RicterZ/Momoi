import json
from typing import Any

from momoi.models import ProviderResponse, ToolCall
from momoi.runtime.context_planner import CONTEXT_PLAN_TOOL_NAME
from momoi.runtime.turns import CONTEXT_PLANNER_SYSTEM_PROMPT


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
            "recall_queries": [message["text"] or "non-text owner message"],
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
        return await self.delegate.complete(  # type: ignore[attr-defined,no-any-return]
            system, messages, tools, **kwargs
        )


def with_context_planner(provider: object) -> PlannerAwareProvider:
    return PlannerAwareProvider(provider)
