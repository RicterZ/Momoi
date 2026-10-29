from datetime import datetime, timezone

from ..contracts.balance import Balance
from .deepseek import DeepSeekAccounting


# Standard paid tier, USD per 1M tokens: cache hit, input, output (thinking included).
_GEMINI_RATES = {
    "gemini-3.6-flash": (
        (datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp(), (0.075, 0.75, 3.75)),
        (float("inf"), (0.15, 1.50, 7.50)),
    ),
}


def _gemini_model(model: str) -> str:
    return model.removeprefix("models/").lower()


class GeminiAccounting(DeepSeekAccounting):
    """Gemini API list prices; other models keep the DeepSeek estimate."""

    def __init__(self, usd_cny_rate: float = 7.1) -> None:
        self.usd_cny_rate = float(usd_cny_rate)

    def token_rates(self, model: str, timestamp: float) -> tuple[float, float, float]:
        periods = _GEMINI_RATES.get(_gemini_model(model))
        if periods is None:
            return super().token_rates(model, timestamp)
        rates = next(rates for until, rates in periods if timestamp < until)
        return tuple(rate * self.usd_cny_rate for rate in rates)


class GoogleAIStudioBalanceProvider:
    def __init__(self, *, usd_cny_rate: float = 7.1, accounting: bool = True) -> None:
        if type(accounting) is not bool:
            raise ValueError("accounting must be boolean")
        rate = float(usd_cny_rate)
        if not rate > 0:
            raise ValueError("usd_cny_rate must be positive")
        self.accounting = GeminiAccounting(rate) if accounting else None

    async def balance(self) -> Balance:
        # Google AI Studio has no account balance API.
        return {"source": "disabled", "currency": "CNY", "is_available": False, "total_balance": "0"}
