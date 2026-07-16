from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, Transaction, to_cents


def test_to_cents() -> None:
    assert to_cents(Decimal("-42.50")) == -4250
    assert to_cents(Decimal("1234567.89")) == 123456789


async def test_account_and_transaction_roundtrip(session: AsyncSession) -> None:
    acct = Account(
        aggregator_id="ACT-1",
        name="Checking",
        org_name="Chase",
        type=AccountType.checking,
        currency="USD",
        balance_cents=to_cents(Decimal("1234.56")),
        balance_date=date(2026, 7, 1),
    )
    session.add(acct)
    await session.flush()
    txn = Transaction(
        account_id=acct.id,
        aggregator_id="TRN-1",
        posted_on=date(2026, 7, 2),
        amount_cents=-4250,
        description="NETFLIX.COM",
        raw={"id": "TRN-1"},
    )
    session.add(txn)
    await session.commit()
    got = (await session.execute(select(Transaction))).scalar_one()
    assert got.amount_cents == -4250
    assert got.merchant is None and got.series_id is None
    assert got.raw["id"] == "TRN-1"
