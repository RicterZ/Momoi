"""Per-provider pricing strategies; usage rows keep the key they were billed with."""

from typing import Any
from urllib.parse import urlsplit

from ..llm.accounting import UsageAccounting

PRICING_STRATEGIES = ("deepseek", "google")
DEFAULT_PRICING = "deepseek"
DEFAULT_USD_CNY_RATE = 7.1


def pricing_key(options: dict[str, Any]) -> str | None:
    """Stable key for a model provider's pricing choice, or None when unset."""
    name = str(options.get("pricing") or "")
    if not name:
        return None
    if name not in PRICING_STRATEGIES:
        raise ValueError(f"pricing must be empty or one of {', '.join(PRICING_STRATEGIES)}")
    if name == "google":
        rate = float(options.get("usd_cny_rate", DEFAULT_USD_CNY_RATE))
        if not rate > 0:
            raise ValueError("usd_cny_rate must be positive")
        return f"google:{rate:g}"
    return name


_HOST_PRICING = {
    "api.deepseek.com": "deepseek",
    "generativelanguage.googleapis.com": "google",
}


def provider_pricing(options: dict[str, Any]) -> str | None:
    """Explicit pricing, else the strategy implied by a known vendor address."""
    key = pricing_key(options)
    if key:
        return key
    host = urlsplit(str(options.get("base_url") or "")).hostname or ""
    name = _HOST_PRICING.get(host.lower())
    return pricing_key({**options, "pricing": name}) if name else None


def legacy_pricing_key(adapter: str, options: dict[str, Any]) -> str | None:
    """Key equivalent to an old global balance binding; None for plugin strategies."""
    if not options.get("accounting", True) or adapter == "deepseek":
        return DEFAULT_PRICING
    if adapter == "google":
        return pricing_key({"pricing": "google", "usd_cny_rate": options.get("usd_cny_rate", DEFAULT_USD_CNY_RATE)})
    return None


def accounting_for(key: str | None) -> UsageAccounting | None:
    from .adapters.deepseek import DeepSeekAccounting
    from .adapters.google import GeminiAccounting

    name, _, argument = str(key or "").partition(":")
    if name == "deepseek":
        return DeepSeekAccounting()
    if name == "google":
        try:
            rate = float(argument) if argument else DEFAULT_USD_CNY_RATE
        except ValueError:
            return None
        return GeminiAccounting(rate) if rate > 0 else None
    return None


def balance_for(key: str | None, options: dict[str, Any], transport=None):
    """Balance provider reusing the model provider key; None when the vendor has no balance API."""
    if str(key or "").partition(":")[0] != "deepseek":
        return None
    from .adapters.deepseek import DeepSeekBalanceProvider

    return DeepSeekBalanceProvider(
        api_key=str(options.get("api_key") or ""),
        base_url=str(options.get("base_url") or "https://api.deepseek.com"),
        transport=transport,
    )


class PricingBook(UsageAccounting):
    """Price each usage row with the strategy recorded on it."""

    def __init__(self, fallback: UsageAccounting | None = None) -> None:
        # Rows without a recognized key keep the legacy global strategy.
        self.fallback = fallback or accounting_for(DEFAULT_PRICING)
        self._strategies: dict[str, UsageAccounting] = {}

    def strategy(self, key: str | None) -> UsageAccounting:
        if not key:
            return self.fallback
        if key not in self._strategies:
            self._strategies[key] = accounting_for(key) or self.fallback
        return self._strategies[key]

    def parse_usage(self, data):
        return self.fallback.parse_usage(data)

    def token_rates(self, model: str, timestamp: float) -> tuple[float, float, float]:
        return self.strategy(None).token_rates(model, timestamp)

    def estimate_cost(self, model, timestamp, *, cache_read, uncached, cache_write=0, output, pricing=None) -> float:
        return self.strategy(pricing).estimate_cost(
            model, timestamp, cache_read=cache_read, uncached=uncached,
            cache_write=cache_write, output=output,
        )


UNSUPPORTED_BALANCE = {"source": "unsupported", "currency": "CNY", "is_available": False, "total_balance": "0"}


class Billing:
    """Dashboard billing view: per-row pricing plus an optional vendor balance."""

    def __init__(self, accounting: PricingBook, balance_source=None) -> None:
        self.accounting = accounting
        self.balance_source = balance_source

    @property
    def balance_supported(self) -> bool:
        return self.balance_source is not None

    async def balance(self):
        if self.balance_source is None:
            return dict(UNSUPPORTED_BALANCE)
        return await self.balance_source.balance()


def priced_usage_sink(sink, key):
    if sink is None or not key:
        return sink

    def record(**kwargs):
        kwargs.setdefault("pricing", key)
        return sink(**kwargs)

    return record


def priced_metrics_sink(sink, key):
    if sink is None or not key:
        return sink

    def record(data):
        return sink(data if data.get("pricing") else {**data, "pricing": key})

    return record
