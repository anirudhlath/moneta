from datetime import date

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.models import Account, AccountType, TransferLink
from moneta.views.cashflow import accrual_by_month, accrual_income, accrual_spend, cash_out
from tests.factories import make_account, make_txn


async def _cc_purchase_and_payment(session: AsyncSession) -> tuple[Account, Account]:
    """Checking+credit accounts: one credit purchase, and a checking->credit CC payment link."""
    checking = await make_account(session, type=AccountType.checking)
    credit = await make_account(session, type=AccountType.credit)
    await make_txn(
        session, credit, amount_cents=-8000, posted_on=date(2026, 7, 2), description="RESTAURANT"
    )
    out = await make_txn(
        session, checking, amount_cents=-8000, posted_on=date(2026, 7, 5), description="CC PAYMENT"
    )
    inn = await make_txn(
        session,
        credit,
        amount_cents=8000,
        posted_on=date(2026, 7, 5),
        description="PAYMENT THANK YOU",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    return checking, credit


async def test_accrual_counts_cc_purchases_not_payments(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == 8000


async def test_cash_out_counts_cc_payment_not_purchase(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    assert await cash_out(session, date(2026, 7, 1), date(2026, 7, 31)) == 8000


async def test_internal_moves_count_nowhere(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session, checking, amount_cents=-10000, posted_on=date(2026, 7, 3), description="TO SAVINGS"
    )
    inn = await make_txn(
        session,
        savings,
        amount_cents=10000,
        posted_on=date(2026, 7, 3),
        description="FROM CHECKING",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == 0
    assert await cash_out(session, date(2026, 7, 1), date(2026, 7, 31)) == 0


async def test_cashflow_endpoint_returns_accrual_and_cash_out(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _cc_purchase_and_payment(session)
    await session.commit()

    app = create_app(sessionmaker, adapter=None, llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/cashflow", params={"start": "2026-07-01", "end": "2026-07-31"})

    assert r.status_code == 200
    body = r.json()
    assert body["start"] == "2026-07-01"
    assert body["end"] == "2026-07-31"
    assert body["accrual_cents"] == 8000  # the RESTAURANT purchase
    assert body["cash_out_cents"] == 8000  # the CC PAYMENT, not the purchase


async def test_loan_account_purchase_not_accrual(session: AsyncSession) -> None:
    loan = await make_account(session, type=AccountType.loan)
    await make_txn(
        session,
        loan,
        amount_cents=-120000,
        posted_on=date(2026, 7, 2),
        description="FURNITURE STORE FINANCED PURCHASE",
    )
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == 0


async def test_accrual_income_counts_paycheck_inflow(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 2),
        description="ACME CORP PAYROLL",
    )
    assert await accrual_income(session, date(2026, 7, 1), date(2026, 7, 31)) == 250000


async def test_accrual_income_excludes_linked_inflow(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    # the only positive-amount txn in this fixture is the CC payment's inflow leg
    # (credit account, +8000, transfer-linked) — it must not count as income.
    assert await accrual_income(session, date(2026, 7, 1), date(2026, 7, 31)) == 0


async def test_accrual_by_month_buckets_multiple_months(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session, checking, amount_cents=250000, posted_on=date(2026, 7, 5), description="PAYROLL"
    )
    await make_txn(
        session, checking, amount_cents=-5000, posted_on=date(2026, 7, 10), description="GROCERY"
    )
    await make_txn(
        session, checking, amount_cents=200000, posted_on=date(2026, 6, 5), description="PAYROLL"
    )
    await make_txn(
        session, checking, amount_cents=-4000, posted_on=date(2026, 6, 20), description="GROCERY"
    )
    rows = await accrual_by_month(session, months=2, today=date(2026, 7, 20))
    assert rows == [
        ("2026-07", 250000, 5000),
        ("2026-06", 200000, 4000),
    ]


async def test_accrual_by_month_excludes_linked_txns(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    # same fixture as test_accrual_counts_cc_purchases_not_payments/
    # test_accrual_income_excludes_linked_inflow: RESTAURANT counts as spend,
    # the CC PAYMENT's linked legs count nowhere.
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 8000)]
