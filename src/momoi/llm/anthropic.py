import logging
from pathlib import Path
from time import time
from typing import Any, Callable

import aiohttp

from .dumps import dump_request
from .errors import ProviderError, ProviderResponseError
from .telemetry import (
    anthropic_reasoning,
    compact_response_text,
    log_tool_schema,
    persist_thinking,
    record_response,
    thinking_effort,
)
from .transport import anthropic_url, http_error, retry_request
from ..config.models import LLMConfig
from ..observability.events import log_event
from ..models import ProviderResponse, ToolCall

logger = logging.getLogger(__name__)


def merge_adjacent_roles(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine neighbouring same-role messages for alternating-role APIs."""
    merged: list[dict[str, Any]] = []
    for message in messages:
        previous = merged[-1] if merged else None
        if previous is None or previous.get("role") != message.get("role"):
            merged.append(dict(message))
            continue
        before, after = previous.get("content"), message.get("content")
        if isinstance(before, str) and isinstance(after, str):
            previous["content"] = f"{before}\n{after}"
            continue
        previous["content"] = [
            *(
                before
                if isinstance(before, list)
                else [{"type": "text", "text": before}]
            ),
            *(after if isinstance(after, list) else [{"type": "text", "text": after}]),
        ]
    return merged


class AnthropicProvider:
    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self.config = config
        self.dump_dir = dump_dir
        self.usage_sink: Callable[..., None] | None = None
        self.usage_parser: (
            Callable[[dict[str, Any]], dict[str, float | int | bool] | None] | None
        ) = None
        self.thinking_sink: Callable[..., None] | None = None
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AnthropicProvider":
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()

    def update_config(self, config: LLMConfig) -> None:
        if config.api_format != "anthropic":
            raise ValueError("changing llm.api_format requires a restart")
        self.config = config

    async def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        require_tool: bool = False,
        required_tool: str | None = None,
    ) -> ProviderResponse:
        if self._session is None:
            raise RuntimeError("provider is not started")
        config = self.config
        payload: dict[str, Any] = {
            "model": config.model,
            "system": system,
            "messages": merge_adjacent_roles(messages),
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        effort = thinking_effort(config)
        if effort:
            payload.pop("temperature", None)
            payload["output_config"] = {"effort": effort}
        if tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool["input_schema"],
                }
                for tool in tools
            ]
            if config.tool_choice and required_tool:
                payload["tool_choice"] = {
                    "type": "tool",
                    "name": required_tool,
                }
            elif require_tool and config.tool_choice:
                payload["tool_choice"] = {"type": "any"}
        log_tool_schema("anthropic", payload.get("tools"))
        dump_path = dump_request(self.dump_dir, "anthropic", payload, require_tool)
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async def request(attempt_started: float) -> ProviderResponse:
            billed_at = time()
            assert self._session is not None
            async with self._session.post(
                anthropic_url(config.base_url, "/messages"),
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    raise await http_error(response, "Anthropic-compatible")
                try:
                    data = await response.json()
                except (aiohttp.ClientError, ValueError) as error:
                    raise ProviderResponseError(
                        "Anthropic-compatible endpoint returned invalid JSON"
                    ) from error
                if not isinstance(data, dict):
                    raise ProviderResponseError(
                        "Anthropic-compatible endpoint returned non-object JSON"
                    )
                duration_ms, metrics = record_response(
                    data,
                    protocol="anthropic",
                    attempt_started=attempt_started,
                    dump_path=dump_path,
                    usage_sink=self.usage_sink,
                    model=config.model,
                    parse_usage=self.usage_parser,
                    created_at=billed_at,
                )
                content = [
                    block
                    for block in data.get("content", [])
                    if isinstance(block, dict)
                ]
                tool_calls = []
                for block in content:
                    if block.get("type") != "tool_use":
                        continue
                    raw_input = block.get("input")
                    tool_calls.append(
                        ToolCall(
                            str(block.get("id") or ""),
                            str(block.get("name") or ""),
                            raw_input if isinstance(raw_input, dict) else {},
                            (
                                None
                                if isinstance(raw_input, dict)
                                else "tool_arguments_must_be_object"
                            ),
                        )
                    )
                persist_thinking(
                    self.thinking_sink,
                    reasoning=anthropic_reasoning(content),
                    tools=[call.name for call in tool_calls],
                    model=config.model,
                )
                if tool_calls:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "llm_response",
                        protocol="anthropic",
                        response_kind="tools",
                        tool_count=len(tool_calls),
                        tool_names=",".join(call.name for call in tool_calls),
                        duration_ms=duration_ms,
                    )
                    return ProviderResponse(content, tool_calls, metrics)
                text = "\n".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                ).strip()
                if not text:
                    raise ProviderError(
                        "Anthropic-compatible endpoint returned no text content"
                    )
                log_event(
                    logger,
                    logging.DEBUG,
                    "llm_response",
                    protocol="anthropic",
                    response_kind="text",
                    text=compact_response_text(text),
                    duration_ms=duration_ms,
                )
                return ProviderResponse(content, [], metrics)

        return await retry_request(
            protocol="anthropic",
            max_retries=config.max_retries,
            request_fields={
                "model": config.model,
                "messages": len(messages),
                "tools": len(tools or []),
                "require_tool": require_tool,
                "thinking_effort": effort or None,
            },
            operation=request,
        )
