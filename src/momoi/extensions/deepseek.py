from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from .base import UsagePlugin


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
