from abc import ABC, abstractmethod


class UsagePlugin(ABC):
    """Dashboard usage plugin. Return only the data the page needs."""

    async def balance(self) -> dict[str, object]:
        """Account funds. Must include source, currency, is_available, total_balance."""
        raise NotImplementedError

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
