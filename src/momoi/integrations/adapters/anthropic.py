from typing import Any

from ...llm.errors import ProviderError
from ...llm.telemetry import anthropic_reasoning
from ...llm.transport import anthropic_url
from ...models import ProviderResponse, ToolCall
from .protocol_base import ProtocolProvider


def merge_adjacent_roles(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine neighbouring same-role messages for alternating-role APIs."""
    merged: list[dict[str, Any]] = []
    for message in messages:
        message = {
            key: value for key, value in message.items()
            if key != "provider_continuation" and not key.startswith("_")
        }
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


class AnthropicProvider(ProtocolProvider):
    protocol = "anthropic"
    label = "Anthropic-compatible"

    def _url(self) -> str:
        return anthropic_url(self.config.base_url, "/messages")

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _payload(self, system, messages, tools, *, require_tool, required_tool, effort):
        config = self.config
        payload: dict[str, Any] = {
            "model": config.model,
            "system": system,
            "messages": merge_adjacent_roles(messages),
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        if effort == "off":
            payload["thinking"] = {"type": "disabled"}
        elif effort:
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
        return payload

    def _parse(self, data, response) -> ProviderResponse:
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
        return ProviderResponse(content, tool_calls)

    def _check_content(self, parsed: ProviderResponse) -> None:
        if not parsed.tool_calls and not "\n".join(
            block.get("text", "")
            for block in parsed.content
            if block.get("type") == "text"
        ).strip():
            raise ProviderError(
                "Anthropic-compatible endpoint returned no text content"
            )

    def _thinking(self, parsed: ProviderResponse) -> str:
        return anthropic_reasoning(parsed.content)
