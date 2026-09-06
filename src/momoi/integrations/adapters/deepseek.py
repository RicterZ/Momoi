from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from decimal import Decimal, InvalidOperation

from ..transport import HTTPTransport
from ..contracts.balance import Balance
from ..errors import IntegrationError, ErrorCategory, http_category, error_category

from ...llm.accounting import (
    UsageAccounting,
    billed_usage,
    usage_int,
    usage_mapping,
)


# DeepSeek publishes its billing windows in Beijing time. This is a provider
# pricing rule, not Momoi's application/display timezone.
DEEPSEEK_BILLING_TIMEZONE = ZoneInfo("Asia/Shanghai")
PEAK_START = datetime(2026, 8, 17, tzinfo=DEEPSEEK_BILLING_TIMEZONE)
_RATES = {
    "deepseek-v4-flash": {
        "flat": (0.02, 1.0, 2.0),
        "offpeak": (0.05, 1.5, 4.5),
        "peak": (0.10, 3.0, 9.0),
    },
    "deepseek-v4-pro": {
        "flat": (0.025, 3.0, 6.0),
        "offpeak": (0.15, 4.5, 13.5),
        "peak": (0.30, 9.0, 27.0),
    },
}


def _root_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


class DeepSeekBalanceProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 10,
        transport: HTTPTransport | None = None,
    ) -> None:
        self.transport = transport or HTTPTransport()
        self.base_url = _root_url(str(base_url))
        self.api_key = str(api_key)
        self.timeout_seconds = float(timeout_seconds)

    async def balance(self) -> Balance:
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with self.transport.session(
                timeout_seconds=self.timeout_seconds
            ) as session:
                async with session.get(
                    f"{self.base_url}/user/balance",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise IntegrationError(
                            f"DeepSeek balance HTTP {response.status}",
                            category=http_category(response.status),
                            service="deepseek",
                            operation="balance",
                            retryable=response.status >= 500 or response.status == 429,
                        )
                    payload = await response.json(content_type=None)
            infos = payload.get("balance_infos") if isinstance(payload, dict) else None
            if (
                not isinstance(infos, list)
                or not infos
                or not isinstance(infos[0], dict)
                or type(payload.get("is_available")) is not bool
            ):
                raise ValueError("invalid balance response")
            info = infos[0]
            amount = str(info["total_balance"])
            if not Decimal(amount).is_finite() or not isinstance(
                info.get("currency"), str
            ):
                raise ValueError("invalid balance amount or currency")
            return {
                "source": "live",
                "currency": info["currency"],
                "is_available": payload["is_available"],
                "total_balance": amount,
            }
        except IntegrationError:
            raise
        except (ValueError, KeyError, InvalidOperation) as error:
            raise IntegrationError(
                "DeepSeek balance returned an invalid response",
                category=ErrorCategory.INVALID_RESPONSE,
                service="deepseek",
                operation="balance",
            ) from error
        except Exception as error:
            raise IntegrationError(
                f"DeepSeek balance request failed: {type(error).__name__}",
                category=error_category(error),
                service="deepseek",
                operation="balance",
            ) from error


class DeepSeekAccounting(UsageAccounting):
    def token_rates(self, model: str, timestamp: float) -> tuple[float, float, float]:
        table = _RATES.get(model) or _RATES["deepseek-v4-flash"]
        when = datetime.fromtimestamp(timestamp, DEEPSEEK_BILLING_TIMEZONE)
        if when < PEAK_START:
            return table["flat"]
        minutes = when.hour * 60 + when.minute
        peak = 9 * 60 <= minutes < 12 * 60 or 14 * 60 <= minutes < 18 * 60
        return table["peak"] if peak else table["offpeak"]

    def parse_usage(self, data: dict[str, Any]) -> dict[str, float | int | bool] | None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        return _parse_deepseek_usage(usage)


def _parse_deepseek_usage(usage: dict[str, Any]) -> dict[str, float | int | bool]:
    prompt_details = usage_mapping(usage.get("prompt_tokens_details"))
    completion_details = usage_mapping(usage.get("completion_tokens_details"))
    cache_read = usage_int(usage.get("prompt_cache_hit_tokens"))
    if cache_read == 0:
        cache_read = usage_int(prompt_details.get("cached_tokens"))
    if cache_read == 0:
        cache_read = usage_int(usage.get("cache_read_input_tokens"))

    miss = usage_int(usage.get("prompt_cache_miss_tokens"))
    prompt = usage_int(usage.get("prompt_tokens"))
    if miss:
        uncached = miss
        cache_write = 0
    elif prompt:
        cache_write = usage_int(prompt_details.get("cache_write_tokens"))
        uncached = max(0, prompt - cache_read - cache_write)
    else:
        cache_write = usage_int(usage.get("cache_creation_input_tokens"))
        uncached = usage_int(usage.get("input_tokens"))
    input_tokens = uncached + cache_read + cache_write
    if prompt > input_tokens:
        input_tokens = prompt

    output = max(
        usage_int(usage.get("completion_tokens")),
        usage_int(usage.get("output_tokens")),
    )
    reasoning = usage_int(completion_details.get("reasoning_tokens"))
    total = usage_int(usage.get("total_tokens"))
    if total > input_tokens:
        output = max(output, total - input_tokens)
    elif "completion_tokens" not in usage and reasoning:
        output += reasoning

    return billed_usage(
        input_tokens=input_tokens,
        uncached=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        output=output,
        cache_reported=any(
            key in usage
            for key in (
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        or "cached_tokens" in prompt_details
        or "cache_write_tokens" in prompt_details,
    )
