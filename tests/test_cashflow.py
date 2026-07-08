from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.models import Account, AccountType, TransferLink
from moneta.views.cashflow import accrual_spend, cash_out
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
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == Decimal("80")


async def test_cash_out_counts_cc_payment_not_purchase(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    assert await cash_out(session, date(2026, 7, 1), date(2026, 7, 31)) == Decimal("80")


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
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == Decimal("0")
    assert await cash_out(session, date(2026, 7, 1), date(2026, 7, 31)) == Decimal("0")


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
    assert Decimal(body["accrual"]) == Decimal("80")  # the RESTAURANT purchase
    assert Decimal(body["cash_out"]) == Decimal("80")  # the CC PAYMENT, not the purchase


async def test_loan_account_purchase_not_accrual(session: AsyncSession) -> None:
    loan = await make_account(session, type=AccountType.loan)
    await make_txn(
        session,
        loan,
        amount_cents=-120000,
        posted_on=date(2026, 7, 2),
        description="FURNITURE STORE FINANCED PURCHASE",
    )
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == Decimal("0")
