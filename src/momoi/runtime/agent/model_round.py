import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...observability.context import log_context, new_trace_id
from ...models import ProviderResponse
from ...storage import estimate_tokens
from .context_window import ContextWindow
from .protocol import owner_request_messages


Complete = Callable[
    [list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool],
    Awaitable[ProviderResponse],
]
SystemPolicy = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]
]


@dataclass(frozen=True)
class ModelRoundResult:
    response: ProviderResponse
    request_tools: list[dict[str, Any]]
    call_id: str
    history_messages: int
    remind_owner_bubbles: bool


class ModelRoundRunner:
    """Build, execute, and account for one provider request."""

    def __init__(self, context_window: ContextWindow, store: Any):
        self.context_window = context_window
        self.store = store

    async def run(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        request_tools: list[dict[str, Any]],
        *,
        complete: Complete,
        system_policy: SystemPolicy | None,
        authority: str,
        remind_owner_bubbles: bool,
        harness_started: bool,
        require_tool: bool,
        history_messages: int,
        stage: str,
        turn_id: str,
        round_number: int,
        channel: str,
        goal_id: str | None,
    ) -> ModelRoundResult:
        request_system = (
            system_policy(system, request_tools)
            if system_policy is not None
            else system
        )
        call_id = new_trace_id()
        with log_context(
            stage=stage,
            turn_id=turn_id,
            call_id=call_id,
            round=round_number,
            channel=channel,
            goal_id=goal_id,
        ):
            history_messages = self.context_window.fit(
                request_system,
                messages,
                request_tools,
                history_messages,
            )
            request_messages = (
                owner_request_messages(
                    messages,
                    remind_bubbles=remind_owner_bubbles and harness_started,
                )
                if authority == "owner"
                else messages
            )
            self.context_window.check_budget(
                turn_id, request_system, request_messages, request_tools
            )
            response = await complete(
                request_system,
                request_messages,
                request_tools,
                require_tool,
            )

        metrics = response.usage or {}
        input_tokens = int(
            metrics.get(
                "input",
                estimate_tokens(
                    json.dumps(
                        {
                            "system": request_system,
                            "messages": request_messages,
                            "tools": request_tools,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                ),
            )
        )
        output_tokens = int(
            metrics.get(
                "output",
                estimate_tokens(
                    json.dumps(response.content, ensure_ascii=False, default=str)
                ),
            )
        )
        self.store.record_turn_usage(turn_id, input_tokens, output_tokens)
        return ModelRoundResult(
            response=response,
            request_tools=request_tools,
            call_id=call_id,
            history_messages=history_messages,
            remind_owner_bubbles=(
                authority == "owner"
                and not any(call.name == "send_bubbles" for call in response.tool_calls)
            ),
        )
