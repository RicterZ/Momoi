import json
import logging
from pathlib import Path
from time import time
from typing import Any, Callable

import aiohttp

from ...llm.dumps import dump_request
from ...llm.errors import ProviderResponseError
from ...llm.telemetry import (
    compact_response_text,
    log_tool_schema,
    openai_reasoning,
    persist_thinking,
    record_response,
    thinking_effort,
)
from ...llm.transport import http_error, openai_url, retry_request
from ...integrations.models import LLMConfig
from ...observability.events import log_event
from ...observability.values import safe_preview
from ...models import ProviderResponse, ToolCall

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
        if isinstance(content, str):
            wire.append({"role": role, "content": content})
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


class OpenAIProvider:
    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self.config = config
        self.accounting = None
        self.dump_dir = dump_dir
        self.usage_sink: Callable[..., None] | None = None
        self.usage_parser: (
            Callable[[dict[str, Any]], dict[str, float | int | bool] | None] | None
        ) = None
        self.thinking_sink: Callable[..., None] | None = None
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "OpenAIProvider":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()

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
            "messages": openai_messages(system, messages),
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        effort = thinking_effort(config)
        if effort:
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
        log_tool_schema("openai", payload.get("tools"))
        dump_path = dump_request(self.dump_dir, "openai", payload, require_tool)
        headers = {
            "authorization": f"Bearer {config.api_key}",
            "content-type": "application/json",
        }

        async def request(attempt_started: float) -> ProviderResponse:
            billed_at = time()
            assert self._session is not None
            async with self._session.post(
                openai_url(config.base_url),
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    raise await http_error(response, "OpenAI-compatible")
                try:
                    data = await response.json()
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
                duration_ms, metrics = record_response(
                    data,
                    protocol="openai",
                    attempt_started=attempt_started,
                    dump_path=dump_path,
                    usage_sink=self.usage_sink,
                    model=config.model,
                    parse_usage=self.usage_parser,
                    created_at=billed_at,
                )
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
                persist_thinking(
                    self.thinking_sink,
                    reasoning=reasoning,
                    tools=[call.name for call in tool_calls],
                    model=config.model,
                )
                if tool_calls:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "llm_response",
                        protocol="openai",
                        response_kind="tools",
                        tool_count=len(tool_calls),
                        tool_names=",".join(call.name for call in tool_calls),
                        duration_ms=duration_ms,
                    )
                    return ProviderResponse(
                        content,
                        tool_calls,
                        metrics,
                        reasoning=reasoning,
                    )
                if not content:
                    raise ProviderResponseError(
                        "OpenAI-compatible endpoint returned no text content"
                    )
                log_event(
                    logger,
                    logging.DEBUG,
                    "llm_response",
                    protocol="openai",
                    response_kind="text",
                    text=compact_response_text(str(text)),
                    duration_ms=duration_ms,
                )
                return ProviderResponse(content, [], metrics, reasoning=reasoning)

        return await retry_request(
            protocol="openai",
            max_retries=config.max_retries,
            request_fields={
                "model": config.model,
                "messages": len(messages),
                "tools": len(tools or []),
                "require_tool": require_tool,
                "tool_choice": payload.get("tool_choice"),
                "thinking_effort": effort or None,
            },
            operation=request,
        )
