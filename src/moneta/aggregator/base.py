import asyncio
from collections.abc import Awaitable, Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel

from moneta.models import AccountType


class AccountDTO(BaseModel):
    id: str
    name: str
    org_name: str
    currency: str
    balance: Decimal
    balance_date: date
    type_hint: AccountType | None = None


class TransactionDTO(BaseModel):
    id: str
    account_id: str
    posted_on: date
    amount: Decimal
    description: str
    raw: dict[str, Any]


class HoldingDTO(BaseModel):
    account_id: str
    symbol: str
    quantity: float
    market_value: Decimal


class Snapshot(BaseModel):
    accounts: list[AccountDTO]
    transactions: list[TransactionDTO]
    holdings: list[HoldingDTO]


class AggregatorAdapter(Protocol):
    async def fetch(self, since: date | None = None) -> Snapshot: ...


async def gather_snapshots(fetches: Iterable[Awaitable[Snapshot]]) -> Snapshot:
    """Run fetches concurrently and concatenate the results.

    return_exceptions lets every sibling settle before the first failure is
    raised — a bare gather would leave the others running unawaited.
    """
    results = await asyncio.gather(*fetches, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    snaps = [r for r in results if isinstance(r, Snapshot)]
    return Snapshot(
        accounts=[a for s in snaps for a in s.accounts],
        transactions=[t for s in snaps for t in s.transactions],
        holdings=[h for s in snaps for h in s.holdings],
    )


class MergedAdapter:
    """Fans fetch() out to several adapters and concatenates their snapshots."""

    def __init__(self, adapters: Sequence[AggregatorAdapter]) -> None:
        self._adapters = list(adapters)

    async def fetch(self, since: date | None = None) -> Snapshot:
        return await gather_snapshots(a.fetch(since) for a in self._adapters)
