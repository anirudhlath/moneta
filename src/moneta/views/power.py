from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    Cadence,
    Direction,
    RecurringSeries,
    SeriesStatus,
    Transaction,
    TransferLink,
    from_cents,
)
from moneta.pipelines.recurring import monthly_cents

_SPEND_TYPES = (AccountType.checking, AccountType.savings, AccountType.credit)


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


async def _credit_paying_series_ids(session: AsyncSession) -> set[int]:
    """Series whose txns are transfer-linked into a credit account (CC balance payments)."""
    linked_inflow_by_series: set[int] = set()
    links = (await session.execute(select(TransferLink))).scalars().all()
    if not links:
        return set()
    txn_series = {
        tid: sid
        for tid, sid in (await session.execute(select(Transaction.id, Transaction.series_id))).all()
    }
    txn_account = {
        tid: aid
        for tid, aid in (
            await session.execute(select(Transaction.id, Transaction.account_id))
        ).all()
    }
    acct_type = {a.id: a.type for a in (await session.execute(select(Account))).scalars()}
    for link in links:
        sid = txn_series.get(link.outflow_id)
        inflow_acct = txn_account.get(link.inflow_id)
        if (
            sid is not None
            and inflow_acct is not None
            and acct_type.get(inflow_acct) == AccountType.credit
        ):
            linked_inflow_by_series.add(sid)
    return linked_inflow_by_series


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
    cc_series = await _credit_paying_series_ids(session)

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

    linked_ids: set[int] = set()
    for link in (await session.execute(select(TransferLink))).scalars():
        linked_ids.update((link.outflow_id, link.inflow_id))
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
                    Account.type.in_(_SPEND_TYPES),
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
