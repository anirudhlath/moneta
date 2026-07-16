from collections.abc import Iterable
from datetime import date

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
)
from moneta.pipelines.recurring import monthly_cents
from moneta.queries import classified_links, linked_txn_ids, primary_currency


class SeriesLine(BaseModel):
    merchant: str
    cadence: Cadence
    monthly_cents: int


class PowerReport(BaseModel):
    month: str
    monthly_income_cents: int
    income_sources: list[SeriesLine]
    fixed_costs: list[SeriesLine]
    total_fixed_cents: int
    spending_power_cents: int
    spent_so_far_cents: int
    remaining_cents: int


def _series_lines(series: Iterable[RecurringSeries]) -> tuple[list[SeriesLine], int]:
    lines = [
        SeriesLine(merchant=s.merchant, cadence=s.cadence, monthly_cents=abs(monthly_cents(s)))
        for s in series
    ]
    lines.sort(key=lambda line: line.monthly_cents, reverse=True)
    return lines, sum(line.monthly_cents for line in lines)


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

    income, monthly_income = _series_lines(s for s in series if s.direction == Direction.inflow)
    fixed, total_fixed = _series_lines(
        s for s in series if s.direction == Direction.outflow and s.id not in cc_series
    )

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

    power = monthly_income - total_fixed
    return PowerReport(
        month=f"{today.year:04d}-{today.month:02d}",
        monthly_income_cents=monthly_income,
        income_sources=income,
        fixed_costs=fixed,
        total_fixed_cents=total_fixed,
        spending_power_cents=power,
        spent_so_far_cents=spent_cents,
        remaining_cents=power - spent_cents,
    )
