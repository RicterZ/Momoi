from ...llm.streaming import response_data
import json
import logging
from urllib.parse import urlsplit
from typing import Any

import aiohttp

from ...llm.errors import ProviderResponseError
from ...llm.telemetry import openai_reasoning
from ...llm.transport import openai_url
from ...observability.events import log_event
from ...observability.values import safe_preview
from ...models import ProviderResponse, ToolCall
from .protocol_base import ProtocolProvider

logger = logging.getLogger(__name__)


def _text(content: Any, separator: str = "\n") -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return separator.join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def openai_messages(
    system: str | list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    system_text = _text(system, "\n\n")
    if system_text:
        wire.append({"role": "system", "content": system_text})
    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content")
        continuation = message.get("provider_continuation", {}).get("openai", {})
        if isinstance(content, str):
            item = {"role": role, "content": content}
            if role == "assistant" and "reasoning_content" in continuation:
                item["reasoning_content"] = continuation["reasoning_content"]
            wire.append(item)
            continue
        if not isinstance(content, list):
            continue
        text = _text(content)
        if role == "assistant":
            reasoning = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "reasoning"
                and str(block.get("text") or "").strip()
            )
            tool_calls = [
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            item: dict[str, Any] = {"role": "assistant", "content": text or None}
            if reasoning:
                item["reasoning_content"] = reasoning
            if "reasoning_content" in continuation:
                item["reasoning_content"] = continuation["reasoning_content"]
            if tool_calls:
                item["tool_calls"] = tool_calls
            wire.append(item)
            continue
        wire.extend(
            {
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id") or ""),
                "content": str(block.get("content") or ""),
            }
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
        parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                value = str(block.get("text") or "")
                if not value:
                    continue
                if parts and parts[-1].get("type") == "text":
                    parts[-1]["text"] = f"{parts[-1]['text']}\n{value}"
                else:
                    parts.append({"type": "text", "text": value})
                continue
            if block.get("type") != "image":
                continue
            source = block.get("source")
            if not isinstance(source, dict):
                continue
            if source.get("type") == "url" and isinstance(source.get("url"), str):
                url = source["url"]
            elif source.get("type") == "base64" and isinstance(source.get("data"), str):
                url = (
                    f"data:{source.get('media_type') or 'image/jpeg'};"
                    f"base64,{source['data']}"
                )
            else:
                continue
            parts.append({"type": "image_url", "image_url": {"url": url}})
        if any(part.get("type") == "image_url" for part in parts):
            wire.append({"role": role, "content": parts})
        elif text:
            wire.append({"role": role, "content": text})
    return wire


def _log_unusable_response(
    response: aiohttp.ClientResponse,
    data: Any,
    reason: str,
) -> None:
    if isinstance(data, dict):
        choices = data.get("choices")
        choices_type = type(choices).__name__
        choices_length = len(choices) if isinstance(choices, list) else -1
        keys = ",".join(sorted(str(key) for key in data)) or "<none>"
        error = data.get("error")
    else:
        choices_type = "<unknown>"
        choices_length = -1
        keys = "<non-object>"
        error = None
    log_event(
        logger,
        logging.WARNING,
        "llm_response_unusable",
        protocol="openai",
        status=response.status,
        reason=reason,
        keys=keys,
        choices_type=choices_type,
        choices_count=choices_length,
        error=safe_preview(error, 300),
        body=safe_preview(data, 1000),
    )


class OpenAIProvider(ProtocolProvider):
    """OpenAI Chat Completions protocol adapter."""

    protocol = "openai"
    label = "OpenAI-compatible"

    def _url(self) -> str:
        return openai_url(self.config.base_url)

    def _payload(self, system, messages, tools, *, require_tool, required_tool, effort):
        config = self.config
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": openai_messages(system, messages),
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        host = urlsplit(config.base_url).hostname or ""
        model = config.model.lower()
        if host == "generativelanguage.googleapis.com" or model.startswith("gemini"):
            # Gemini's OpenAI layer rejects unknown fields such as `thinking`
            # and accepts only none/minimal/low/medium/high.
            if effort and effort != "off":
                payload.pop("temperature", None)
            if effort:
                payload["reasoning_effort"] = "none" if effort == "off" else "high" if effort in {"xhigh", "max"} else effort
        elif effort == "off":
            # Native OpenAI uses `none`; DeepSeek-compatible APIs use their
            # thinking switch and reject a simultaneous reasoning_effort.
            native_openai = host == "api.openai.com" or model.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4"))
            if native_openai:
                payload["reasoning_effort"] = "none"
            else:
                payload["thinking"] = {"type": "disabled"}
        elif effort:
            payload.pop("temperature", None)
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tools
            ]
            if config.tool_choice and required_tool:
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": required_tool},
                }
            elif require_tool and config.tool_choice:
                payload["tool_choice"] = "required"
        return payload

    async def _response_json(self, response):
        try:
            data = await response_data(response, self.protocol)
        except (aiohttp.ClientError, ValueError) as error:
            body = await response.text()
            _log_unusable_response(response, body, "invalid_json")
            raise ProviderResponseError(
                "OpenAI-compatible endpoint returned invalid JSON: "
                f"{type(error).__name__}"
            ) from error
        if not isinstance(data, dict):
            _log_unusable_response(response, data, "non_object_json")
            raise ProviderResponseError(
                "OpenAI-compatible endpoint returned non-object JSON"
            )
        return data

    def _parse(self, data, response) -> ProviderResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            _log_unusable_response(response, data, "no_choices")
            raise ProviderResponseError(
                "OpenAI-compatible endpoint returned no choices"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            _log_unusable_response(response, data, "no_message")
            raise ProviderResponseError(
                "OpenAI-compatible endpoint returned no message"
            )
        content: list[dict[str, Any]] = []
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            content.append({"type": "text", "text": text})
        tool_calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") if isinstance(item, dict) else None
            if not isinstance(function, dict):
                continue
            raw_arguments = function.get("arguments")
            argument_error = None
            try:
                if not isinstance(raw_arguments, str):
                    raise TypeError
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {}
                argument_error = "invalid_tool_arguments_json"
            except TypeError:
                arguments = {}
                argument_error = "tool_arguments_must_be_json"
            if not isinstance(arguments, dict):
                arguments = {}
                argument_error = "tool_arguments_must_be_object"
            call = ToolCall(
                str(item.get("id") or ""),
                str(function.get("name") or ""),
                arguments,
                argument_error,
            )
            tool_calls.append(call)
            content.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        reasoning = openai_reasoning(message)
        continuation = (
            {"openai": {"reasoning_content": (
                message["reasoning_content"]
                if isinstance(message.get("reasoning_content"), str)
                else reasoning
            )}}
            if reasoning or isinstance(message.get("reasoning_content"), str)
            else {}
        )
        return ProviderResponse(content, tool_calls, reasoning=reasoning, continuation=continuation)

    def _check_content(self, parsed: ProviderResponse) -> None:
        if not parsed.content:
            raise ProviderResponseError(
                "OpenAI-compatible endpoint returned no text content"
            )
