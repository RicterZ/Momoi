import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .config import LLMConfig
from .models import ProviderResponse, ToolCall
from .text_replacement import cyber_keyword_pre_hook

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class ProviderResponseError(ProviderError):
    """The endpoint returned a successful but unusable response."""


def _api_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"


def _openai_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    prefix = "" if urlsplit(base).path.rstrip("/") else "/v1"
    return f"{base}{prefix}/chat/completions"


def usage_metrics(data: dict[str, Any]) -> dict[str, float | int | bool] | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None

    def count(name: str) -> int:
        value = usage.get(name, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    if "prompt_tokens" in usage:
        input_total = count("prompt_tokens")
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cached = details.get("cached_tokens", usage.get("prompt_cache_hit_tokens", 0))
        cache_read = cached if isinstance(cached, int) and cached >= 0 else 0
        written = details.get("cache_write_tokens", 0)
        cache_write = written if isinstance(written, int) and written >= 0 else 0
        output = count("completion_tokens")
        return {
            "input": input_total,
            "uncached": max(0, input_total - cache_read - cache_write),
            "cache_read": cache_read,
            "cache_write": cache_write,
            "output": output,
            "total": input_total + output,
            "cache_hit_rate": cache_read / input_total * 100 if input_total else 0.0,
            "cache_reported": (
                "cached_tokens" in details
                or "prompt_cache_hit_tokens" in usage
                or "cache_write_tokens" in details
            ),
        }

    uncached = count("input_tokens")
    cache_read = count("cache_read_input_tokens")
    cache_write = count("cache_creation_input_tokens")
    cache_reported = any(
        name in usage
        for name in ("cache_read_input_tokens", "cache_creation_input_tokens")
    )
    output = count("output_tokens")
    input_total = uncached + cache_read + cache_write
    return {
        "input": input_total,
        "uncached": uncached,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": output,
        "total": input_total + output,
        "cache_hit_rate": cache_read / input_total * 100 if input_total else 0.0,
        "cache_reported": cache_reported,
    }


def _log_usage(data: dict[str, Any]) -> None:
    metrics = usage_metrics(data)
    if not metrics:
        logger.debug("LLM usage unavailable")
    elif metrics["cache_reported"]:
        logger.debug(
            "LLM usage input=%d uncached=%d cache_read=%d cache_write=%d "
            "output=%d total=%d cache_hit=%.1f%%",
            metrics["input"],
            metrics["uncached"],
            metrics["cache_read"],
            metrics["cache_write"],
            metrics["output"],
            metrics["total"],
            metrics["cache_hit_rate"],
        )
    else:
        logger.debug(
            "LLM usage input=%d output=%d total=%d cache=not_reported",
            metrics["input"],
            metrics["output"],
            metrics["total"],
        )


def _dump_request(
    dump_dir: Path | None,
    enabled: bool,
    provider: str,
    payload: dict[str, Any],
    require_tool: bool,
) -> None:
    if not enabled or dump_dir is None:
        return
    try:
        timestamp = datetime.now(timezone.utc)
        dump_dir.mkdir(parents=True, exist_ok=True)
        path = dump_dir / (
            f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}.json"
        )
        path.write_text(
            json.dumps(
                {
                    "timestamp": timestamp.isoformat(),
                    "provider": provider,
                    "require_tool": require_tool,
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        logger.warning("failed to dump LLM request", exc_info=True)


async def _http_error(response: aiohttp.ClientResponse, protocol: str) -> ProviderError:
    body = await response.text()
    try:
        error_data = json.loads(body)
        detail = str(error_data.get("error", {}).get("message", ""))[:300]
    except (ValueError, TypeError, AttributeError):
        detail = body.strip()[:300]
    suffix = f": {detail}" if detail else ""
    return ProviderError(f"{protocol} endpoint returned HTTP {response.status}{suffix}")


class AnthropicProvider:
    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self.config = config
        self.dump_dir = dump_dir
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AnthropicProvider":
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
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
    ) -> ProviderResponse:
        if self._session is None:
            raise RuntimeError("provider is not started")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "system": system,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools
            if require_tool:
                payload["tool_choice"] = {"type": "any"}
        payload = cyber_keyword_pre_hook.replace_strings(payload)
        _dump_request(self.dump_dir, self.config.dump_prompts, "anthropic", payload, require_tool)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        logger.debug(
            "LLM request model=%s messages=%d tools=%d",
            self.config.model,
            len(messages),
            len(tools or []),
        )
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                async with self._session.post(
                    _api_url(self.config.base_url, "/messages"), json=payload, headers=headers
                ) as response:
                    if response.status >= 500 and attempt < self.config.max_retries:
                        await response.read()
                        await asyncio.sleep(min(2**attempt, 5))
                        continue
                    if response.status != 200:
                        raise await _http_error(response, "Anthropic-compatible")
                    data = await response.json()
                    _log_usage(data)
                    content = [
                        block
                        for block in data.get("content", [])
                        if isinstance(block, dict)
                    ]
                    tool_calls = [
                        ToolCall(
                            str(block.get("id") or ""),
                            str(block.get("name") or ""),
                            block.get("input") if isinstance(block.get("input"), dict) else {},
                        )
                        for block in content
                        if block.get("type") == "tool_use"
                    ]
                    if tool_calls:
                        logger.debug(
                            "LLM requested tools=%s",
                            ",".join(call.name for call in tool_calls),
                        )
                        return ProviderResponse(content, tool_calls, usage_metrics(data))
                    text = "\n".join(
                        block.get("text", "")
                        for block in content
                        if block.get("type") == "text"
                    ).strip()
                    if not text:
                        raise ProviderError("Anthropic-compatible endpoint returned no text content")
                    logger.debug(
                        "LLM response text=%s", json.dumps(text, ensure_ascii=False)
                    )
                    return ProviderResponse(content, [], usage_metrics(data))
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                if attempt < self.config.max_retries:
                    await asyncio.sleep(min(2**attempt, 5))
                    continue
        raise ProviderError(f"Anthropic-compatible request failed: {type(last_error).__name__}")


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


def _openai_messages(
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
            tool_calls = [
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            item: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                item["tool_calls"] = tool_calls
            wire.append(item)
            continue
        image_parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            source = block.get("source")
            if not isinstance(source, dict):
                continue
            if source.get("type") == "url" and isinstance(source.get("url"), str):
                url = source["url"]
            elif source.get("type") == "base64" and isinstance(source.get("data"), str):
                url = f"data:{source.get('media_type') or 'image/jpeg'};base64,{source['data']}"
            else:
                continue
            image_parts.append({"type": "image_url", "image_url": {"url": url}})
        if image_parts:
            parts = ([{"type": "text", "text": text}] if text else []) + image_parts
            wire.append({"role": role, "content": parts})
        elif text:
            wire.append({"role": role, "content": text})
        wire.extend(
            {
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id") or ""),
                "content": str(block.get("content") or ""),
            }
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
    return wire


class OpenAIProvider:
    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self.config = config
        self.dump_dir = dump_dir
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
    ) -> ProviderResponse:
        if self._session is None:
            raise RuntimeError("provider is not started")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": _openai_messages(system, messages),
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
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
            if require_tool:
                payload["tool_choice"] = "required"
        payload = cyber_keyword_pre_hook.replace_strings(payload)
        _dump_request(self.dump_dir, self.config.dump_prompts, "openai", payload, require_tool)
        headers = {
            "authorization": f"Bearer {self.config.api_key}",
            "content-type": "application/json",
        }
        logger.debug(
            "LLM request model=%s messages=%d tools=%d",
            self.config.model,
            len(messages),
            len(tools or []),
        )
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                async with self._session.post(
                    _openai_url(self.config.base_url),
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status >= 500 and attempt < self.config.max_retries:
                        await response.read()
                        await asyncio.sleep(min(2**attempt, 5))
                        continue
                    if response.status != 200:
                        raise await _http_error(response, "OpenAI-compatible")
                    data = await response.json()
                    _log_usage(data)
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices:
                        raise ProviderResponseError(
                            "OpenAI-compatible endpoint returned no choices"
                        )
                    message = choices[0].get("message")
                    if not isinstance(message, dict):
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
                        try:
                            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
                        except json.JSONDecodeError:
                            arguments = {}
                        arguments = arguments if isinstance(arguments, dict) else {}
                        call = ToolCall(
                            str(item.get("id") or ""),
                            str(function.get("name") or ""),
                            arguments,
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
                    if tool_calls:
                        logger.debug(
                            "LLM requested tools=%s",
                            ",".join(call.name for call in tool_calls),
                        )
                        return ProviderResponse(content, tool_calls, usage_metrics(data))
                    if not content:
                        raise ProviderResponseError(
                            "OpenAI-compatible endpoint returned no text content"
                        )
                    logger.debug("LLM response text=%s", json.dumps(text, ensure_ascii=False))
                    return ProviderResponse(content, [], usage_metrics(data))
            except ProviderResponseError as error:
                last_error = error
                if attempt < self.config.max_retries:
                    logger.warning(
                        "OpenAI-compatible endpoint returned an unusable response; retrying attempt=%d/%d error=%s",
                        attempt + 1,
                        self.config.max_retries,
                        error,
                    )
                    await asyncio.sleep(min(2**attempt, 5))
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                if attempt < self.config.max_retries:
                    await asyncio.sleep(min(2**attempt, 5))
                    continue
        raise ProviderError(f"OpenAI-compatible request failed: {type(last_error).__name__}")
