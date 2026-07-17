import asyncio
from collections.abc import Awaitable, Iterable
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, Field

from moneta.models import AccountType


class AccountDTO(BaseModel):
    id: str
    name: str
    org_name: str
    currency: str
    balance: Decimal
    balance_date: date
    type_hint: AccountType | None = None
    source: str = ""


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
    warnings: list[str] = Field(default_factory=list)


class AggregatorAdapter(Protocol):
    @property
    def source(self) -> str: ...  # "simplefin" / "plaid" / a test fake's own name

    async def fetch(self, since: date | None = None) -> Snapshot: ...


def concat_snapshots(snaps: list[Snapshot]) -> Snapshot:
    """Merge snapshots into one, concatenating all four fields in order."""
    return Snapshot(
        accounts=[a for s in snaps for a in s.accounts],
        transactions=[t for s in snaps for t in s.transactions],
        holdings=[h for s in snaps for h in s.holdings],
        warnings=[w for s in snaps for w in s.warnings],
    )


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
    return concat_snapshots(snaps)
