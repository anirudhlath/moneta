from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, Transaction

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
