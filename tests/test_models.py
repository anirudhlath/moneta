from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    Transaction,
    bill_ref,
    recurring_cluster_item,
    same_bill,
    to_cents,
)


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


def test_bill_ref_reads_optional_amount() -> None:
    assert bill_ref({"merchant": "Acme", "direction": "outflow"}) == ("Acme", "outflow", None)
    assert bill_ref({"merchant": "Acme", "direction": "outflow", "amount_cents": 999}) == (
        "Acme",
        "outflow",
        999,
    )
    assert bill_ref({"merchant": "Acme", "direction": "outflow", "amount_cents": -5}) == (
        "Acme",
        "outflow",
        None,
    )
    assert bill_ref({"merchant": "Acme", "direction": "sideways"}) is None


def test_same_bill_tolerance_and_merchant_level() -> None:
    assert same_bill(("Acme", "outflow", None), ("Acme", "outflow", 999))
    assert same_bill(("Acme", "outflow", 1000), ("Acme", "outflow", 1200))
    assert not same_bill(("Acme", "outflow", 1000), ("Acme", "outflow", 1300))
    assert not same_bill(("Acme", "outflow", None), ("Acme", "inflow", None))
    assert not same_bill(("Acme", "outflow", None), ("Acmes", "outflow", None))


def test_recurring_cluster_item_names_amount_only_when_given() -> None:
    plain = recurring_cluster_item("Acme", "outflow")
    assert plain.question == "Is 'Acme' a recurring bill?"
    assert plain.payload == {"merchant": "Acme", "direction": "outflow"}
    priced = recurring_cluster_item("Acme", "outflow", 4999)
    assert priced.question == "Is 'Acme' ($49.99) a recurring bill?"
    assert priced.payload == {"merchant": "Acme", "direction": "outflow", "amount_cents": 4999}
