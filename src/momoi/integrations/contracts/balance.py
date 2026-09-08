from typing import Protocol, TypedDict
from ...llm.accounting import UsageAccounting


class Balance(TypedDict):
    source: str
    currency: str
    is_available: bool
    total_balance: str


class BalanceProvider(Protocol):
    accounting: UsageAccounting | None

    async def balance(self) -> Balance: ...
