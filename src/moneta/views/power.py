from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    SPEND_ACCOUNT_TYPES,
    Account,
    AccountType,
    Cadence,
    Direction,
    RecurringSeries,
    SeriesStatus,
    Transaction,
    from_cents,
)
from moneta.pipelines.recurring import monthly_cents
from moneta.queries import classified_links, linked_txn_ids, primary_currency


class FixedCostLine(BaseModel):
    merchant: str
    cadence: Cadence
    monthly_amount: Decimal


class PowerReport(BaseModel):
    month: str
    monthly_income: Decimal
    fixed_costs: list[FixedCostLine]
    total_fixed: Decimal
    spending_power: Decimal
    spent_so_far: Decimal
    remaining: Decimal


async def power_report(session: AsyncSession, today: date) -> PowerReport:
    month_start = today.replace(day=1)
    series = (
        (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.active)
            )
        )
        .scalars()
        .all()
    )
    links = await classified_links(session)
    cc_series = {
        link.outflow_series_id
        for link in links
        if link.outflow_series_id is not None and link.inflow_account_type == AccountType.credit
    }

    income_cents = sum(monthly_cents(s) for s in series if s.direction == Direction.inflow)
    fixed = [
        FixedCostLine(
            merchant=s.merchant,
            cadence=s.cadence,
            monthly_amount=from_cents(abs(monthly_cents(s))),
        )
        for s in series
        if s.direction == Direction.outflow and s.id not in cc_series
    ]
    fixed.sort(key=lambda line: line.monthly_amount, reverse=True)
    total_fixed = sum((line.monthly_amount for line in fixed), Decimal(0))

    linked_ids = linked_txn_ids(links)
    primary = await primary_currency(session)
    month_txns = (
        (
            await session.execute(
                select(Transaction)
                .join(Account, Transaction.account_id == Account.id)
                .where(
                    Transaction.amount_cents < 0,
                    Transaction.posted_on >= month_start,
                    Transaction.posted_on <= today,
                    Transaction.series_id.is_(None),
                    Account.type.in_(SPEND_ACCOUNT_TYPES),
                    Account.currency == primary,
                )
            )
        )
        .scalars()
        .all()
    )
    spent_cents = sum(-t.amount_cents for t in month_txns if t.id not in linked_ids)

    income = from_cents(income_cents)
    spent = from_cents(spent_cents)
    power = income - total_fixed
    return PowerReport(
        month=f"{today.year:04d}-{today.month:02d}",
        monthly_income=income,
        fixed_costs=fixed,
        total_fixed=total_fixed,
        spending_power=power,
        spent_so_far=spent,
        remaining=power - spent,
    )
