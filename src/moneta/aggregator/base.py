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
