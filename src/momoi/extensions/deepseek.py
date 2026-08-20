from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .base import (
    UsagePlugin,
    billed_usage,
    parse_protocol_usage,
    usage_int,
    usage_mapping,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
PEAK_START = datetime(2026, 8, 17, tzinfo=SHANGHAI)
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


class DeepSeekPlugin(UsagePlugin):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 10,
    ) -> None:
        self.base_url = _root_url(str(base_url))
        self.api_key = str(api_key)
        self.timeout_seconds = min(20.0, max(1.0, float(timeout_seconds)))

    def token_rates(self, model: str, timestamp: float) -> tuple[float, float, float]:
        table = _RATES.get(model) or _RATES["deepseek-v4-flash"]
        when = datetime.fromtimestamp(timestamp, SHANGHAI)
        if when < PEAK_START:
            return table["flat"]
        minutes = when.hour * 60 + when.minute
        peak = 9 * 60 <= minutes < 12 * 60 or 14 * 60 <= minutes < 18 * 60
        return table["peak"] if peak else table["offpeak"]

    def parse_usage(
        self, data: dict[str, Any]
    ) -> dict[str, float | int | bool] | None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
            return _parse_deepseek_usage(usage)
        return parse_protocol_usage(data)

    async def balance(self) -> dict[str, object]:
        if not self.api_key:
            return {
                "source": "unavailable",
                "currency": "CNY",
                "is_available": False,
                "total_balance": "0",
            }
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}/user/balance",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as response:
                    status = response.status
                    payload = await response.json(content_type=None)
            if status != 200 or not isinstance(payload, dict):
                return {
                    "source": "unavailable",
                    "currency": "CNY",
                    "is_available": False,
                    "total_balance": "0",
                }
            infos = payload.get("balance_infos")
            info = (
                infos[0]
                if isinstance(infos, list) and infos and isinstance(infos[0], dict)
                else {}
            )
            return {
                "source": "live",
                "currency": str(info.get("currency") or "CNY"),
                "is_available": bool(payload.get("is_available")),
                "total_balance": str(info.get("total_balance") or "0"),
            }
        except Exception:
            return {
                "source": "unavailable",
                "currency": "CNY",
                "is_available": False,
                "total_balance": "0",
            }


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
    if reasoning:
        if total and total == input_tokens + output + reasoning:
            output += reasoning
        elif "completion_tokens" not in usage and output < reasoning:
            output += reasoning

    return billed_usage(
        input_tokens=input_tokens,
        uncached=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        output=output,
        cache_reported=True,
    )
