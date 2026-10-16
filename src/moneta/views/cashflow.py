from datetime import date
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import month_bounds
from moneta.logical import load_logical_data
from moneta.models import (
    LIQUID_ACCOUNT_TYPES,
    SPEND_ACCOUNT_TYPES,
    Direction,
    RecurringSeries,
)
from moneta.queries import ClassifiedLink, classified_links, linked_txn_ids, primary_currency


class MonthFlows(NamedTuple):
    """One calendar month of `power --history`. Every amount is an unsigned magnitude."""

    month: str  # "YYYY-MM"
    income_cents: int  # inflows tagged to an income series (non-discretionary, active or ended)
    other_inflows_cents: int  # every other counted inflow: refunds, P2P, one-off deposits
    spend_cents: int


async def _accrual(
    session: AsyncSession,
    start: date,
    end: date,
    links: list[ClassifiedLink] | None,
    primary: str | None,
    *,
    inflows: bool,
) -> int:
    """Shared magnitude sum behind accrual_spend/accrual_inflows: same exclusions
    (transfer-linked txns, non-spend accounts, non-primary currency), opposite
    amount sign. Both callers return an unsigned magnitude."""
    if links is None:
        links = await classified_links(session)
    excluded = linked_txn_ids(links)
    if primary is None:
        primary = await primary_currency(session)
    logical = await load_logical_data(session)
    txns = [
        t
        for t in logical.transactions
        if start <= t.posted_on <= end
        and logical.accounts[logical.account_roots[t.account_id]].type in SPEND_ACCOUNT_TYPES
        and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        and (t.amount_cents > 0 if inflows else t.amount_cents < 0)
    ]
    sign = 1 if inflows else -1
    return sum(sign * t.amount_cents for t in txns if t.id not in excluded)


async def accrual_spend(
    session: AsyncSession,
    start: date,
    end: date,
    links: list[ClassifiedLink] | None = None,
    primary: str | None = None,
) -> int:
    return await _accrual(session, start, end, links, primary, inflows=False)


async def accrual_inflows(
    session: AsyncSession,
    start: date,
    end: date,
    links: list[ClassifiedLink] | None = None,
    primary: str | None = None,
) -> int:
    """Every counted inflow, income or not: history's income plus its other inflows."""
    return await _accrual(session, start, end, links, primary, inflows=True)


async def accrual_by_month(
    session: AsyncSession,
    months: int,
    today: date,
    links: list[ClassifiedLink] | None = None,
    primary: str | None = None,
) -> list[MonthFlows]:
    """Flows per calendar month, newest first (index 0 = today's own, still-in-progress
    month), for the trailing `months` months. Income is observed, not projected:
    inflows tagged to an income series (a non-discretionary inflow series, active or
    ended), so a past paycheck counts in its own month. Every other inflow is
    reported beside it, never dropped. One SELECT over the whole
    [oldest month start, newest month end] range instead of accrual_inflows/
    accrual_spend called once per month; same exclusions (transfer-linked txns,
    non-spend accounts, non-primary currency), linked_txn_ids computed once."""
    if links is None:
        links = await classified_links(session)
    excluded = linked_txn_ids(links)
    if primary is None:
        primary = await primary_currency(session)
    income_series = {
        s.id
        for s in await session.scalars(select(RecurringSeries))
        if s.direction == Direction.inflow and not s.discretionary
    }

    bounds: list[tuple[str, date, date]] = []
    year, month = today.year, today.month
    for _ in range(months):
        start, end = month_bounds(date(year, month, 1))
        bounds.append((f"{year:04d}-{month:02d}", start, end))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    oldest_start = bounds[-1][1]
    newest_end = bounds[0][2]

    logical = await load_logical_data(session)
    txns = [
        t
        for t in logical.transactions
        if oldest_start <= t.posted_on <= newest_end
        and logical.accounts[logical.account_roots[t.account_id]].type in SPEND_ACCOUNT_TYPES
        and logical.accounts[logical.account_roots[t.account_id]].currency == primary
    ]
    label_by_ym = {(start.year, start.month): label for label, start, _ in bounds}
    income_by_month = dict.fromkeys(label_by_ym.values(), 0)
    other_by_month = dict.fromkeys(label_by_ym.values(), 0)
    spend_by_month = dict.fromkeys(label_by_ym.values(), 0)
    for t in txns:
        if t.id in excluded:
            continue
        label = label_by_ym.get((t.posted_on.year, t.posted_on.month))
        if label is None:
            continue
        if t.amount_cents > 0 and t.series_id in income_series:
            income_by_month[label] += t.amount_cents
        elif t.amount_cents > 0:
            other_by_month[label] += t.amount_cents
        elif t.amount_cents < 0:
            spend_by_month[label] += -t.amount_cents

    return [
        MonthFlows(label, income_by_month[label], other_by_month[label], spend_by_month[label])
        for label, _, _ in bounds
    ]


async def cash_out(
    session: AsyncSession,
    start: date,
    end: date,
    links: list[ClassifiedLink] | None = None,
    primary: str | None = None,
) -> int:
    if links is None:
        links = await classified_links(session)
    by_outflow = {link.outflow_id: link for link in links}
    if primary is None:
        primary = await primary_currency(session)
    logical = await load_logical_data(session)
    txns = [
        t
        for t in logical.transactions
        if start <= t.posted_on <= end
        and logical.accounts[logical.account_roots[t.account_id]].type in LIQUID_ACCOUNT_TYPES
        and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        and t.amount_cents < 0
    ]
    total = 0
    for t in txns:
        link = by_outflow.get(t.id)
        if link is not None and link.inflow_account_type in LIQUID_ACCOUNT_TYPES:
            continue  # internal liquid->liquid move
        total += -t.amount_cents
    return total
