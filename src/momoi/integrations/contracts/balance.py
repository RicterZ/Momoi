from typing import Protocol, TypedDict


class Balance(TypedDict):
    source: str
    currency: str
    is_available: bool
    total_balance: str


class BalanceProvider(Protocol):
    async def balance(self) -> Balance: ...
