import math
from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    RecurringSeries,
)
from moneta.pipelines.recurring import monthly_cents
from moneta.queries import ClassifiedLink, classified_links


class Obligation(BaseModel):
    account_id: int
    account_name: str
    balance_owed_cents: int
    monthly_payment_cents: int | None
    months_left: int | None
    payoff_estimate: date | None
    promo_expires_on: date | None
    deferred_interest_risk: bool


def _payment_series_id(loan_account_id: int, links: list[ClassifiedLink]) -> int | None:
    """Series of outflows transfer-linked into this loan account, newest first."""
    candidates = [
        link
        for link in links
        if link.inflow_account_id == loan_account_id and link.outflow_series_id is not None
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda link: (link.outflow_posted_on, link.outflow_id))
    return newest.outflow_series_id


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
    links = await classified_links(session)
    result: list[Obligation] = []
    for loan in loans:
        balance_owed_cents = abs(loan.balance_cents)
        payment_cents: int | None = None
        months_left: int | None = None
        payoff: date | None = None
        series_id = _payment_series_id(loan.id, links)
        if series_id is not None:
            series = (
                await session.execute(
                    select(RecurringSeries).where(RecurringSeries.id == series_id)
                )
            ).scalar_one()
            payment_cents = abs(monthly_cents(series))
            if payment_cents > 0:
                months_left = math.ceil(balance_owed_cents / payment_cents)
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
                balance_owed_cents=balance_owed_cents,
                monthly_payment_cents=payment_cents,
                months_left=months_left,
                payoff_estimate=payoff,
                promo_expires_on=loan.promo_expires_on,
                deferred_interest_risk=risk,
            )
        )
    return result
