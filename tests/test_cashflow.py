from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, TransferLink
from moneta.views.cashflow import accrual_spend, cash_out
from tests.factories import make_account, make_txn


async def test_accrual_counts_cc_purchases_not_payments(session: AsyncSession) -> None:
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
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == Decimal("80")


async def test_cash_out_counts_cc_payment_not_purchase(session: AsyncSession) -> None:
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
