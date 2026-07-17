from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import month_bounds
from moneta.models import (
    LIQUID_ACCOUNT_TYPES,
    SPEND_ACCOUNT_TYPES,
    Account,
    Transaction,
)
from moneta.queries import ClassifiedLink, classified_links, linked_txn_ids, primary_currency


async def _accrual(
    session: AsyncSession,
    start: date,
    end: date,
    links: list[ClassifiedLink] | None,
    primary: str | None,
    *,
    inflows: bool,
) -> int:
    """Shared magnitude sum behind accrual_spend/accrual_income: same exclusions
    (transfer-linked txns, non-spend accounts, non-primary currency), opposite
    amount sign. Both callers return an unsigned magnitude."""
    if links is None:
        links = await classified_links(session)
    excluded = linked_txn_ids(links)
    if primary is None:
        primary = await primary_currency(session)
    txns = (
        (
            await session.execute(
                select(Transaction)
                .join(Account, Transaction.account_id == Account.id)
                .where(
                    Transaction.amount_cents > 0 if inflows else Transaction.amount_cents < 0,
                    Transaction.posted_on >= start,
                    Transaction.posted_on <= end,
                    Account.type.in_(SPEND_ACCOUNT_TYPES),
                    Account.currency == primary,
                )
            )
        )
        .scalars()
        .all()
    )
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


async def accrual_income(
    session: AsyncSession,
    start: date,
    end: date,
    links: list[ClassifiedLink] | None = None,
    primary: str | None = None,
) -> int:
    return await _accrual(session, start, end, links, primary, inflows=True)


async def accrual_by_month(
    session: AsyncSession,
    months: int,
    today: date,
    links: list[ClassifiedLink] | None = None,
    primary: str | None = None,
) -> list[tuple[str, int, int]]:
    """Income/spend magnitude per calendar month — (month "YYYY-MM", income_cents,
    spend_cents), newest first (index 0 = today's own, still-in-progress month) —
    for the trailing `months` months. One SELECT over the whole
    [oldest month start, newest month end] range instead of accrual_income/
    accrual_spend called once per month; same exclusions (transfer-linked txns,
    non-spend accounts, non-primary currency), linked_txn_ids computed once."""
    if links is None:
        links = await classified_links(session)
    excluded = linked_txn_ids(links)
    if primary is None:
        primary = await primary_currency(session)

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

    txns = (
        (
            await session.execute(
                select(Transaction)
                .join(Account, Transaction.account_id == Account.id)
                .where(
                    Transaction.posted_on >= oldest_start,
                    Transaction.posted_on <= newest_end,
                    Account.type.in_(SPEND_ACCOUNT_TYPES),
                    Account.currency == primary,
                )
            )
        )
        .scalars()
        .all()
    )
    label_by_ym = {(start.year, start.month): label for label, start, _ in bounds}
    income_by_month = dict.fromkeys(label_by_ym.values(), 0)
    spend_by_month = dict.fromkeys(label_by_ym.values(), 0)
    for t in txns:
        if t.id in excluded:
            continue
        label = label_by_ym.get((t.posted_on.year, t.posted_on.month))
        if label is None:
            continue
        if t.amount_cents > 0:
            income_by_month[label] += t.amount_cents
        elif t.amount_cents < 0:
            spend_by_month[label] += -t.amount_cents

    return [(label, income_by_month[label], spend_by_month[label]) for label, _, _ in bounds]


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
    txns = (
        (
            await session.execute(
                select(Transaction)
                .join(Account, Transaction.account_id == Account.id)
                .where(
                    Transaction.amount_cents < 0,
                    Transaction.posted_on >= start,
                    Transaction.posted_on <= end,
                    Account.type.in_(LIQUID_ACCOUNT_TYPES),
                    Account.currency == primary,
                )
            )
        )
        .scalars()
        .all()
    )
    total = 0
    for t in txns:
        link = by_outflow.get(t.id)
        if link is not None and link.inflow_account_type in LIQUID_ACCOUNT_TYPES:
            continue  # internal liquid->liquid move
        total += -t.amount_cents
    return total
