from collections.abc import Iterable
from datetime import date

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import advance_expected_on, month_bounds, monthly_cents, monthlyize
from moneta.models import (
    Account,
    AccountType,
    Cadence,
    Direction,
    RecurringSeries,
    SeriesStatus,
    Transaction,
)
from moneta.queries import classified_links, loan_payment_stats, primary_currency
from moneta.views.transactions import link_field, spend_reason


class SeriesLine(BaseModel):
    merchant: str
    cadence: Cadence
    monthly_cents: int
    expected_cents: int  # per-cycle magnitude (design 2026-07-16 §3)


class UpcomingCharge(BaseModel):
    merchant: str
    expected_on: date
    expected_cents: int  # magnitude


class PowerReport(BaseModel):
    month: str
    monthly_income_cents: int
    income_sources: list[SeriesLine]
    fixed_costs: list[SeriesLine]
    total_fixed_cents: int
    spending_power_cents: int
    spent_so_far_cents: int
    remaining_cents: int
    days_left: int
    per_day_remaining_cents: int
    upcoming: list[UpcomingCharge]


def _series_lines(series: Iterable[RecurringSeries]) -> tuple[list[SeriesLine], int]:
    lines = [
        SeriesLine(
            merchant=s.merchant,
            cadence=s.cadence,
            monthly_cents=abs(monthly_cents(s)),
            expected_cents=abs(s.expected_cents),
        )
        for s in series
    ]
    lines.sort(key=lambda line: line.monthly_cents, reverse=True)
    return lines, sum(line.monthly_cents for line in lines)


async def power_report(session: AsyncSession, today: date) -> PowerReport:
    month_start, month_end = month_bounds(today)
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
        if link.outflow_series_id is not None
        and link.inflow_account_type == AccountType.credit
        and not link.inflow_is_loan_like
    }

    income, monthly_income = _series_lines(
        s for s in series if s.direction == Direction.inflow and not s.discretionary
    )
    fixed_series = [
        s
        for s in series
        if s.direction == Direction.outflow and s.id not in cc_series and not s.discretionary
    ]
    fixed, total_fixed = _series_lines(fixed_series)

    upcoming = [
        UpcomingCharge(
            merchant=s.merchant,
            expected_on=s.next_expected_on,
            expected_cents=abs(s.expected_cents),
        )
        for s in fixed_series
        if today < s.next_expected_on <= month_end
    ]

    # A loan-like account already represented by an active series in `fixed` must not
    # also get a derived payment line — that would double-count the same obligation.
    fixed_series_ids = {s.id for s in fixed_series}
    covered_loan_accounts = {
        link.inflow_account_id
        for link in links
        if link.inflow_is_loan_like and link.outflow_series_id in fixed_series_ids
    }
    payments = {
        account_id: lp
        for account_id, lp in loan_payment_stats(links).items()
        if account_id not in covered_loan_accounts
    }
    if payments:
        names = {
            aid: name
            for aid, name in (
                await session.execute(
                    select(Account.id, Account.name).where(Account.id.in_(payments))
                )
            ).all()
        }
        for lp in payments.values():
            merchant = f"{names.get(lp.account_id, f'account {lp.account_id}')} — payment"
            line = SeriesLine(
                merchant=merchant,
                cadence=lp.cadence,
                monthly_cents=abs(monthlyize(lp.expected_cents, lp.cadence)),
                expected_cents=abs(lp.expected_cents),
            )
            fixed.append(line)
            total_fixed += line.monthly_cents
            projected = advance_expected_on(lp.last_paid_on, lp.cadence)
            if today < projected <= month_end:
                upcoming.append(
                    UpcomingCharge(
                        merchant=merchant,
                        expected_on=projected,
                        expected_cents=abs(lp.expected_cents),
                    )
                )
        fixed.sort(key=lambda line: line.monthly_cents, reverse=True)
    upcoming.sort(key=lambda u: u.expected_on)

    primary = await primary_currency(session)
    by_outflow = {link.outflow_id: link for link in links}
    inflow_ids = {link.inflow_id for link in links}
    # date-range-only fetch: spend_reason (same predicate transactions.py's trust view
    # uses) decides inclusion — including the inflow/link exclusions — so this can
    # never quietly diverge from what `moneta txns` counts (design 2026-07-16 §2/§3).
    month_txns = (
        await session.execute(
            select(Transaction, Account, RecurringSeries)
            .join(Account, Transaction.account_id == Account.id)
            .outerjoin(RecurringSeries, Transaction.series_id == RecurringSeries.id)
            .where(
                Transaction.posted_on >= month_start,
                Transaction.posted_on <= today,
            )
        )
    ).all()
    spent_cents = sum(
        -txn.amount_cents
        for txn, account, series in month_txns
        if spend_reason(
            txn.amount_cents,
            account.type,
            account.currency,
            primary,
            link_field(txn.id, by_outflow, inflow_ids),
            series.status if series is not None else None,
            series.discretionary if series is not None else None,
        )
        is None
    )

    power = monthly_income - total_fixed
    remaining = power - spent_cents
    days_left = (month_end - today).days + 1
    return PowerReport(
        month=f"{today.year:04d}-{today.month:02d}",
        monthly_income_cents=monthly_income,
        income_sources=income,
        fixed_costs=fixed,
        total_fixed_cents=total_fixed,
        spending_power_cents=power,
        spent_so_far_cents=spent_cents,
        remaining_cents=remaining,
        days_left=days_left,
        per_day_remaining_cents=round(remaining / days_left),
        upcoming=upcoming,
    )
