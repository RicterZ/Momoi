from abc import ABC, abstractmethod
from typing import Any


def usage_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float) and value >= 0:
        as_int = int(value)
        if float(as_int) == value:
            return as_int
    return 0


def usage_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def billed_usage(
    *,
    input_tokens: int,
    uncached: int,
    cache_read: int,
    cache_write: int,
    output: int,
    cache_reported: bool,
) -> dict[str, float | int | bool]:
    return {
        "input": input_tokens,
        "uncached": uncached,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": output,
        "total": input_tokens + output,
        "cache_hit_rate": cache_read / input_tokens * 100 if input_tokens else 0.0,
        "cache_reported": cache_reported,
    }


def parse_protocol_usage(
    data: dict[str, Any],
) -> dict[str, float | int | bool] | None:
    """Normalize OpenAI or Anthropic usage objects into billed token buckets."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None

    details = usage_mapping(usage.get("prompt_tokens_details"))
    cache_creation = usage_mapping(usage.get("cache_creation"))
    if "prompt_tokens" in usage:
        input_tokens = usage_int(usage.get("prompt_tokens"))
        cache_read = usage_int(details.get("cached_tokens"))
        cache_write = usage_int(details.get("cache_write_tokens"))
        uncached = max(0, input_tokens - cache_read - cache_write)
        output = usage_int(usage.get("completion_tokens")) or usage_int(
            usage.get("output_tokens")
        )
        cache_reported = "cached_tokens" in details or "cache_write_tokens" in details
    else:
        uncached = usage_int(usage.get("input_tokens"))
        cache_read = usage_int(usage.get("cache_read_input_tokens"))
        cache_write = usage_int(usage.get("cache_creation_input_tokens"))
        cache_write += usage_int(cache_creation.get("ephemeral_5m_input_tokens"))
        cache_write += usage_int(cache_creation.get("ephemeral_1h_input_tokens"))
        input_tokens = uncached + cache_read + cache_write
        output = usage_int(usage.get("output_tokens")) or usage_int(
            usage.get("completion_tokens")
        )
        cache_reported = any(
            key in usage
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
        ) or bool(cache_creation)
    return billed_usage(
        input_tokens=input_tokens,
        uncached=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        output=output,
        cache_reported=cache_reported,
    )


class UsagePlugin(ABC):
    """Dashboard usage plugin. Return only the data the page needs."""

    async def balance(self) -> dict[str, object]:
        """Account funds. Must include source, currency, is_available, total_balance."""
        raise NotImplementedError

    def parse_usage(
        self, data: dict[str, Any]
    ) -> dict[str, float | int | bool] | None:
        """Normalize one model response into billed token buckets.

        The default parser understands OpenAI and Anthropic usage objects.
        Override this when the provider bills from different usage fields.
        """
        return parse_protocol_usage(data)

    @abstractmethod
    def token_rates(self, model: str, timestamp: float) -> tuple[float, float, float]:
        """CNY per 1M tokens: cache hit, cache miss, output."""

    def estimate_cost(
        self,
        model: str,
        timestamp: float,
        *,
        cache_read: int,
        uncached: int,
        cache_write: int = 0,
        output: int,
    ) -> float:
        hit, miss, completion = self.token_rates(model, timestamp)
        billed_miss = max(0, uncached) + max(0, cache_write)
        return (
            max(0, cache_read) * hit
            + billed_miss * miss
            + max(0, output) * completion
        ) / 1_000_000
