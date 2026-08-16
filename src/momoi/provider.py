import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, time
from typing import Any, Callable
from urllib.parse import urlsplit

import aiohttp

from .config import LLMConfig
from .logging_context import TRACE, current_log_context, log_event, safe_preview
from .models import ProviderResponse, ToolCall
from .text_replacement import cyber_keyword_pre_hook

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class ProviderResponseError(ProviderError):
    """The endpoint returned a successful but unusable response."""


def _retry_delay(attempt: int) -> int:
    return min(2**attempt, 5)


def _log_retry(
    protocol: str,
    attempt: int,
    max_retries: int,
    delay: int,
    *,
    status: int | None = None,
    error: Exception | None = None,
    reason: str | None = None,
) -> None:
    log_event(
        logger,
        logging.WARNING,
        "llm_retry",
        protocol=protocol,
        attempt=attempt + 1,
        attempt_max=max_retries + 1,
        next_attempt=attempt + 2,
        status=status,
        error_type=type(error).__name__ if error is not None else None,
        reason=reason,
        delay_seconds=delay,
    )


def _log_failure(
    protocol: str,
    attempt: int,
    max_retries: int,
    duration_ms: int,
    error: Exception,
    *,
    status: int | None = None,
    reason: str | None = None,
) -> None:
    log_event(
        logger,
        logging.ERROR,
        "llm_failure",
        protocol=protocol,
        attempt=attempt + 1,
        attempt_max=max_retries + 1,
        status=status,
        error_type=type(error).__name__,
        reason=reason,
        duration_ms=duration_ms,
    )


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


def _log_usage(
    data: dict[str, Any], *, protocol: str, duration_ms: int
) -> dict[str, float | int | bool] | None:
    metrics = usage_metrics(data)
    if not metrics:
        log_event(
            logger,
            TRACE,
            "llm_usage",
            protocol=protocol,
            available=False,
            duration_ms=duration_ms,
        )
        return None
    log_event(
        logger,
        TRACE,
        "llm_usage",
        protocol=protocol,
        duration_ms=duration_ms,
        input=metrics["input"],
        uncached=metrics["uncached"],
        cache_read=metrics["cache_read"],
        cache_write=metrics["cache_write"],
        output=metrics["output"],
        total=metrics["total"],
        cache_hit=round(float(metrics["cache_hit_rate"]), 1),
        cache_reported=metrics["cache_reported"],
    )
    return metrics


def _persist_usage(
    sink: Callable[..., None] | None,
    metrics: dict[str, float | int | bool] | None,
    *,
    model: str,
) -> None:
    if sink is None or metrics is None:
        return
    context = current_log_context()
    try:
        sink(
            created_at=time(),
            turn_id=str(context.get("turn_id") or ""),
            stage=str(context.get("stage") or ""),
            model=model,
            metrics=metrics,
        )
    except Exception as error:
        log_event(
            logger,
            logging.WARNING,
            "llm_usage_record_failed",
            error_type=type(error).__name__,
            exc_info=True,
        )


def _dump_request(
    dump_dir: Path | None,
    enabled: bool,
    provider: str,
    payload: dict[str, Any],
    require_tool: bool,
) -> Path | None:
    if not enabled or dump_dir is None:
        return None
    try:
        timestamp = datetime.now(timezone.utc)
        dump_dir.mkdir(parents=True, exist_ok=True)
        path = dump_dir / (
            f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}.json"
        )
        safe_payload = _redact_dump_media(payload)
        path.write_text(
            json.dumps(
                {
                    "timestamp": timestamp.isoformat(),
                    "provider": provider,
                    "require_tool": require_tool,
                    "payload": safe_payload,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path
    except (OSError, TypeError, ValueError) as error:
        log_event(
            logger,
            logging.WARNING,
            "llm_dump_failed",
            error_type=type(error).__name__,
            exc_info=True,
        )
        return None


def _dump_response(path: Path | None, data: Any) -> None:
    if path is None:
        return
    try:
        dumped = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(dumped, dict):
            return
        dumped["response"] = _redact_dump_media(data)
        path.write_text(
            json.dumps(dumped, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as error:
        log_event(
            logger,
            logging.WARNING,
            "llm_dump_failed",
            error_type=type(error).__name__,
            exc_info=True,
        )


def _redact_dump_media(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_dump_media(item) for item in value]
    if isinstance(value, dict):
        redacted = {key: _redact_dump_media(item) for key, item in value.items()}
        if redacted.get("type") == "base64" and isinstance(redacted.get("data"), str):
            redacted["data"] = f"[omitted {len(redacted['data'])} base64 chars]"
        return redacted
    if (
        isinstance(value, str)
        and value.startswith("data:image/")
        and ";base64," in value
    ):
        prefix, encoded = value.split(",", 1)
        return f"{prefix},[omitted {len(encoded)} base64 chars]"
    return value


def _compact_response_text(text: str) -> str:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return json.dumps(text, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _log_openai_unusable_response(
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
        self.usage_sink: Callable[..., None] | None = None
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
        dump_path = _dump_request(
            self.dump_dir, self.config.dump_prompts, "anthropic", payload, require_tool
        )
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            attempt_started = monotonic()
            log_event(
                logger,
                logging.DEBUG,
                "llm_request",
                protocol="anthropic",
                model=self.config.model,
                messages=len(messages),
                tools=len(tools or []),
                require_tool=require_tool,
                attempt=attempt + 1,
                attempt_max=self.config.max_retries + 1,
            )
            try:
                async with self._session.post(
                    _api_url(self.config.base_url, "/messages"), json=payload, headers=headers
                ) as response:
                    if response.status >= 500 and attempt < self.config.max_retries:
                        await response.read()
                        delay = _retry_delay(attempt)
                        _log_retry(
                            "anthropic",
                            attempt,
                            self.config.max_retries,
                            delay,
                            status=response.status,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if response.status != 200:
                        error = await _http_error(response, "Anthropic-compatible")
                        _log_failure(
                            "anthropic",
                            attempt,
                            self.config.max_retries,
                            int((monotonic() - attempt_started) * 1000),
                            error,
                            status=response.status,
                            reason=safe_preview(str(error), 300),
                        )
                        raise error
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
                    _dump_response(dump_path, data)
                    duration_ms = int((monotonic() - attempt_started) * 1000)
                    _persist_usage(
                        self.usage_sink,
                        _log_usage(
                            data, protocol="anthropic", duration_ms=duration_ms
                        ),
                        model=self.config.model,
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
                        return ProviderResponse(content, tool_calls, usage_metrics(data))
                    text = "\n".join(
                        block.get("text", "")
                        for block in content
                        if block.get("type") == "text"
                    ).strip()
                    if not text:
                        error = ProviderError(
                            "Anthropic-compatible endpoint returned no text content"
                        )
                        _log_failure(
                            "anthropic",
                            attempt,
                            self.config.max_retries,
                            duration_ms,
                            error,
                            reason=str(error),
                        )
                        raise error
                    log_event(
                        logger,
                        logging.DEBUG,
                        "llm_response",
                        protocol="anthropic",
                        response_kind="text",
                        text=_compact_response_text(text),
                        duration_ms=duration_ms,
                    )
                    return ProviderResponse(content, [], usage_metrics(data))
            except ProviderResponseError as error:
                last_error = error
                if attempt < self.config.max_retries:
                    delay = _retry_delay(attempt)
                    _log_retry(
                        "anthropic",
                        attempt,
                        self.config.max_retries,
                        delay,
                        error=error,
                        reason=safe_preview(str(error), 300),
                    )
                    await asyncio.sleep(delay)
                    continue
                _log_failure(
                    "anthropic",
                    attempt,
                    self.config.max_retries,
                    int((monotonic() - attempt_started) * 1000),
                    error,
                    reason=safe_preview(str(error), 300),
                )
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                if attempt < self.config.max_retries:
                    delay = _retry_delay(attempt)
                    _log_retry(
                        "anthropic",
                        attempt,
                        self.config.max_retries,
                        delay,
                        error=error,
                    )
                    await asyncio.sleep(delay)
                    continue
                _log_failure(
                    "anthropic",
                    attempt,
                    self.config.max_retries,
                    int((monotonic() - attempt_started) * 1000),
                    error,
                )
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
        wire.extend(
            {
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id") or ""),
                "content": str(block.get("content") or ""),
            }
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
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
    return wire


class OpenAIProvider:
    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self.config = config
        self.dump_dir = dump_dir
        self.usage_sink: Callable[..., None] | None = None
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
            if require_tool and self.config.tool_choice:
                payload["tool_choice"] = "required"
        payload = cyber_keyword_pre_hook.replace_strings(payload)
        dump_path = _dump_request(
            self.dump_dir, self.config.dump_prompts, "openai", payload, require_tool
        )
        headers = {
            "authorization": f"Bearer {self.config.api_key}",
            "content-type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            attempt_started = monotonic()
            log_event(
                logger,
                logging.DEBUG,
                "llm_request",
                protocol="openai",
                model=self.config.model,
                messages=len(messages),
                tools=len(tools or []),
                require_tool=require_tool,
                tool_choice=payload.get("tool_choice"),
                attempt=attempt + 1,
                attempt_max=self.config.max_retries + 1,
            )
            try:
                async with self._session.post(
                    _openai_url(self.config.base_url),
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status >= 500 and attempt < self.config.max_retries:
                        await response.read()
                        delay = _retry_delay(attempt)
                        _log_retry(
                            "openai",
                            attempt,
                            self.config.max_retries,
                            delay,
                            status=response.status,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if response.status != 200:
                        error = await _http_error(response, "OpenAI-compatible")
                        _log_failure(
                            "openai",
                            attempt,
                            self.config.max_retries,
                            int((monotonic() - attempt_started) * 1000),
                            error,
                            status=response.status,
                            reason=safe_preview(str(error), 300),
                        )
                        raise error
                    try:
                        data = await response.json()
                    except (aiohttp.ClientError, ValueError) as error:
                        body = await response.text()
                        _log_openai_unusable_response(response, body, "invalid_json")
                        raise ProviderResponseError(
                            f"OpenAI-compatible endpoint returned invalid JSON: {type(error).__name__}"
                        ) from error
                    if not isinstance(data, dict):
                        _log_openai_unusable_response(response, data, "non_object_json")
                        raise ProviderResponseError(
                            "OpenAI-compatible endpoint returned non-object JSON"
                        )
                    _dump_response(dump_path, data)
                    duration_ms = int((monotonic() - attempt_started) * 1000)
                    _persist_usage(
                        self.usage_sink,
                        _log_usage(data, protocol="openai", duration_ms=duration_ms),
                        model=self.config.model,
                    )
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices:
                        _log_openai_unusable_response(response, data, "no_choices")
                        raise ProviderResponseError(
                            "OpenAI-compatible endpoint returned no choices"
                        )
                    message = choices[0].get("message")
                    if not isinstance(message, dict):
                        _log_openai_unusable_response(response, data, "no_message")
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
                        return ProviderResponse(content, tool_calls, usage_metrics(data))
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
                        text=_compact_response_text(text),
                        duration_ms=duration_ms,
                    )
                    return ProviderResponse(content, [], usage_metrics(data))
            except ProviderResponseError as error:
                last_error = error
                if attempt < self.config.max_retries:
                    delay = _retry_delay(attempt)
                    _log_retry(
                        "openai",
                        attempt,
                        self.config.max_retries,
                        delay,
                        error=error,
                        reason=safe_preview(str(error), 300),
                    )
                    await asyncio.sleep(delay)
                    continue
                _log_failure(
                    "openai",
                    attempt,
                    self.config.max_retries,
                    int((monotonic() - attempt_started) * 1000),
                    error,
                    reason=safe_preview(str(error), 300),
                )
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                if attempt < self.config.max_retries:
                    delay = _retry_delay(attempt)
                    _log_retry(
                        "openai",
                        attempt,
                        self.config.max_retries,
                        delay,
                        error=error,
                    )
                    await asyncio.sleep(delay)
                    continue
                _log_failure(
                    "openai",
                    attempt,
                    self.config.max_retries,
                    int((monotonic() - attempt_started) * 1000),
                    error,
                )
        raise ProviderError(f"OpenAI-compatible request failed: {type(last_error).__name__}")
