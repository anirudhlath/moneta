import math
from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import monthlyize
from moneta.models import Account, AccountType
from moneta.queries import classified_links, loan_payment_stats


class Obligation(BaseModel):
    account_id: int
    account_name: str
    balance_owed_cents: int
    monthly_payment_cents: int | None
    months_left: int | None
    payoff_estimate: date | None
    promo_expires_on: date | None
    deferred_interest_risk: bool


async def compute_obligations(session: AsyncSession, today: date) -> list[Obligation]:
    loans = (
        (
            await session.execute(
                select(Account).where(
                    (Account.type == AccountType.loan) | (Account.financing_mode.is_(True)),
                    Account.balance_cents != 0,
                )
            )
        )
        .scalars()
        .all()
    )
    links = await classified_links(session)
    payments = loan_payment_stats(links)
    result: list[Obligation] = []
    for loan in loans:
        balance_owed_cents = abs(loan.balance_cents)
        months_left: int | None = None
        payoff: date | None = None
        lp = payments.get(loan.id)
        payment_cents = abs(monthlyize(lp.expected_cents, lp.cadence)) if lp else None
        if payment_cents is not None and payment_cents > 0:
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
