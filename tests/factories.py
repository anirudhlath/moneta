from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    Cadence,
    Direction,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    SeriesStatus,
    Transaction,
)

_counter = {"acct": 0, "txn": 0}


async def make_account(session: AsyncSession, **kw: Any) -> Account:
    _counter["acct"] += 1
    defaults: dict[str, Any] = {
        "aggregator_id": f"ACT-{_counter['acct']}",
        "name": f"Account {_counter['acct']}",
        "org_name": "Test Bank",
        "type": AccountType.checking,
        "balance_cents": 0,
        "balance_date": date(2026, 7, 1),
    }
    acct = Account(**{**defaults, **kw})
    session.add(acct)
    await session.flush()
    return acct


async def make_txn(session: AsyncSession, account: Account, **kw: Any) -> Transaction:
    _counter["txn"] += 1
    defaults: dict[str, Any] = {
        "account_id": account.id,
        "aggregator_id": f"TRN-{_counter['txn']}",
        "posted_on": date(2026, 7, 1),
        "amount_cents": -1000,
        "description": "TEST",
        "raw": {},
    }
    txn = Transaction(**{**defaults, **kw})
    session.add(txn)
    await session.flush()
    return txn


async def make_series(session: AsyncSession, **kw: Any) -> RecurringSeries:
    defaults: dict[str, Any] = {
        "merchant": "Netflix",
        "direction": Direction.outflow,
        "cadence": Cadence.monthly,
        "expected_cents": -1599,
        "next_expected_on": date(2026, 8, 1),
        "status": SeriesStatus.active,
    }
    series = RecurringSeries(**{**defaults, **kw})
    session.add(series)
    await session.flush()
    return series


def make_price_change_item(series_id: int, **kw: Any) -> ReviewItem:
    """Unattached price_change ReviewItem; callers add/flush/commit themselves."""
    defaults: dict[str, Any] = {
        "kind": ReviewKind.price_change,
        "question": "Did 'Netflix' change price from $15.99 to $18.99?",
        "payload": {
            "series_id": series_id,
            "merchant": "Netflix",
            "old_cents": -1599,
            "new_cents": -1899,
            "occurred_on": "2026-07-15",
            "llm_flagged": True,
        },
    }
    return ReviewItem(**{**defaults, **kw})
