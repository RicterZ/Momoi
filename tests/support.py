import json
from typing import Any

from momoi.models import ProviderResponse
from momoi.runtime.turns import CONTEXT_PLANNER_SYSTEM_PROMPT


def context_plan_response(messages: list[dict[str, Any]]) -> ProviderResponse:
    payload = json.loads(str(messages[0]["content"]))
    owner_messages = payload["owner_messages"]
    units = [
        {
            "id": f"u{index}",
            "event_ids": [message["event_id"]],
            "text": message["text"],
            "intent": "test owner intent",
            "references": [],
            "recall_queries": [message["text"] or "non-text owner message"],
        }
        for index, message in enumerate(owner_messages, 1)
    ]
    plan = {
        "version": 1,
        "intent_units": units,
        "episode_bindings": [
            {
                "episode_ref": "new:test-thread",
                "title": "Test conversation",
                "relation": "primary",
                "unit_ids": [unit["id"] for unit in units],
                "topics": ["test"],
                "entities": [],
                "open_loops": [],
                "salience": 0.5,
            }
        ],
        "episode_links": [],
        "uncertainty": [],
    }
    return ProviderResponse(
        [{"type": "text", "text": json.dumps(plan, ensure_ascii=False)}], []
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
