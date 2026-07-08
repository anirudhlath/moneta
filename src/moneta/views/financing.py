import math
from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    RecurringSeries,
    Transaction,
    TransferLink,
    from_cents,
)
from moneta.pipelines.recurring import monthly_cents


class Obligation(BaseModel):
    account_id: int
    account_name: str
    balance_owed: Decimal
    monthly_payment: Decimal | None
    months_left: int | None
    payoff_estimate: date | None
    promo_expires_on: date | None
    deferred_interest_risk: bool


async def _payment_series_id(session: AsyncSession, loan_account_id: int) -> int | None:
    """Series of outflows transfer-linked into this loan account, newest first."""
    inflow_txn = select(Transaction.id).where(Transaction.account_id == loan_account_id)
    links = (
        (await session.execute(select(TransferLink).where(TransferLink.inflow_id.in_(inflow_txn))))
        .scalars()
        .all()
    )
    outflow_ids = [link.outflow_id for link in links]
    if not outflow_ids:
        return None
    row = (
        await session.execute(
            select(Transaction.series_id)
            .where(Transaction.id.in_(outflow_ids), Transaction.series_id.is_not(None))
            .order_by(Transaction.posted_on.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def compute_obligations(session: AsyncSession, today: date) -> list[Obligation]:
    loans = (
        (
            await session.execute(
                select(Account).where(Account.type == AccountType.loan, Account.balance_cents != 0)
            )
        )
        .scalars()
        .all()
    )
    result: list[Obligation] = []
    for loan in loans:
        balance_owed = from_cents(abs(loan.balance_cents))
        payment: Decimal | None = None
        months_left: int | None = None
        payoff: date | None = None
        series_id = await _payment_series_id(session, loan.id)
        if series_id is not None:
            series = (
                await session.execute(
                    select(RecurringSeries).where(RecurringSeries.id == series_id)
                )
            ).scalar_one()
            payment = from_cents(abs(monthly_cents(series)))
            if payment > 0:
                months_left = math.ceil(balance_owed / payment)
                payoff = today + timedelta(days=30 * months_left)
        risk = bool(
            payoff is not None
            and loan.promo_expires_on is not None
            and payoff > loan.promo_expires_on
        )
        result.append(
            Obligation(
                account_id=loan.id,
                account_name=loan.name,
                balance_owed=balance_owed,
                monthly_payment=payment,
                months_left=months_left,
                payoff_estimate=payoff,
                promo_expires_on=loan.promo_expires_on,
                deferred_interest_risk=risk,
            )
        )
    return result
