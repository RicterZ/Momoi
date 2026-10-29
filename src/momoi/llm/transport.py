import asyncio
from asyncio import CancelledError, Lock
import json
import logging
from time import monotonic
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

from .errors import ProviderContentFiltered, ProviderError, ProviderResponseError
from .streaming import _stream
from ..integrations.errors import error_category
from ..observability.events import log_event
from ..observability.values import safe_preview
from ..models import ProviderResponse

logger = logging.getLogger(__name__)


class ProviderCircuit:
    """Shared across calls and workflows belonging to one provider instance."""
    def __init__(self):
        self.retry_at = 0.0
        self.lock = Lock()

    async def wait(self):
        async with self.lock:
            remaining = self.retry_at - monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)

    def failed(self, error):
        status = getattr(error, "_http_status", 0)
        self.retry_at = monotonic() + (300 if 400 <= status < 500 and status not in {408, 429} else 60)

    def succeeded(self):
        self.retry_at = 0.0


def anthropic_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"


def openai_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    prefix = "" if urlsplit(base).path.rstrip("/") else "/v1"
    return f"{base}{prefix}/chat/completions"


def responses_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    prefix = "" if urlsplit(base).path.rstrip("/") else "/v1"
    return f"{base}{prefix}/responses"


def should_retry(error: Exception) -> bool:
    return getattr(error, "_http_status", 0) >= 500 or isinstance(
        error,
        (ProviderResponseError, aiohttp.ClientError, asyncio.TimeoutError),
    )


async def retry_request(
    *,
    protocol: str,
    max_retries: int,
    request_fields: dict[str, Any],
    operation: Callable[[float], Awaitable[ProviderResponse]],
    retryable: Callable[[Exception], bool] = should_retry,
    monitor=None,
    circuit=None,
) -> ProviderResponse:
    if circuit is not None:
        await circuit.wait()
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        attempt_started = monotonic()
        log_event(
            logger,
            logging.DEBUG,
            "llm_request",
            protocol=protocol,
            **request_fields,
            attempt=attempt + 1,
            attempt_max=max_retries + 1,
        )
        try:
            if monitor is None:
                result = await operation(attempt_started)
                if circuit is not None:
                    circuit.succeeded()
                return result
            record, token = monitor.begin(attempt + 1)
            try:
                result = await operation(attempt_started)
                if circuit is not None:
                    circuit.succeeded()
                return result
            except BaseException as error:
                record["status"] = "cancelled" if isinstance(error, CancelledError) else "error"
                record["error_type"] = type(error).__name__
                raise
            finally:
                monitor.finish(record, token)
        except (
            ProviderError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as error:
            last_error = error
            status = getattr(error, "_http_status", None)
            reason = (
                safe_preview(str(error), 300)
                if isinstance(error, ProviderError)
                else None
            )
            if attempt < max_retries and _stream.get() is None and retryable(error):
                delay = min(2**attempt, 5)
                log_event(
                    logger,
                    logging.WARNING,
                    "llm_retry",
                    protocol=protocol,
                    model=request_fields.get("model"),
                    attempt=attempt + 1,
                    attempt_max=max_retries + 1,
                    next_attempt=attempt + 2,
                    status=status,
                    error_type=type(error).__name__ if status is None else None,
                    reason=(
                        reason
                        if isinstance(error, ProviderResponseError)
                        else None
                    ),
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)
                continue
            log_event(
                logger,
                logging.ERROR,
                "llm_failure",
                protocol=protocol,
                model=request_fields.get("model"),
                attempt=attempt + 1,
                attempt_max=max_retries + 1,
                status=status,
                error_type=type(error).__name__,
                reason=reason,
                duration_ms=int((monotonic() - attempt_started) * 1000),
            )
            if circuit is not None and not isinstance(error, ProviderContentFiltered):
                circuit.failed(error)
            if isinstance(error, ProviderError):
                raise
            break
    name = {"openai": "OpenAI Chat Completions", "openai_responses": "OpenAI Responses"}.get(protocol, "Anthropic")
    raise ProviderError(
        f"{name}-compatible request failed: {type(last_error).__name__}",
        category=error_category(last_error),
    ) from last_error


async def http_error(
    response: aiohttp.ClientResponse, protocol: str
) -> ProviderError:
    body = await response.text()
    try:
        error_data = json.loads(body)
        detail = str(error_data.get("error", {}).get("message", ""))[:300]
    except (ValueError, TypeError, AttributeError):
        detail = body.strip()[:300]
    suffix = f": {detail}" if detail else ""
    error = ProviderError(
        f"{protocol} endpoint returned HTTP {response.status}{suffix}"
    )
    error._http_status = response.status  # type: ignore[attr-defined]
    return error
