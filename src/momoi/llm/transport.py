import asyncio
import json
import logging
from time import monotonic
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

from .errors import ProviderError, ProviderResponseError
from ..observability.events import log_event
from ..observability.values import safe_preview
from ..models import ProviderResponse

logger = logging.getLogger(__name__)


def anthropic_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"


def openai_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    prefix = "" if urlsplit(base).path.rstrip("/") else "/v1"
    return f"{base}{prefix}/chat/completions"


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
) -> ProviderResponse:
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
            return await operation(attempt_started)
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
            if attempt < max_retries and retryable(error):
                delay = min(2**attempt, 5)
                log_event(
                    logger,
                    logging.WARNING,
                    "llm_retry",
                    protocol=protocol,
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
                attempt=attempt + 1,
                attempt_max=max_retries + 1,
                status=status,
                error_type=type(error).__name__,
                reason=reason,
                duration_ms=int((monotonic() - attempt_started) * 1000),
            )
            if isinstance(error, ProviderError):
                raise
            break
    name = "OpenAI" if protocol == "openai" else "Anthropic"
    raise ProviderError(
        f"{name}-compatible request failed: {type(last_error).__name__}"
    )


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
