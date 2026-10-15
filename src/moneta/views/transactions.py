"""Transaction drill-down: the trust view. Every row shown, never silently hidden;
`counted_in_spend`/`excluded_because` are built from `spend_reason`, the same pure
predicate `views/power.py`'s spent-so-far consumes (design 2026-07-16 §2) — one spend
rule, not two hand-mirrored copies.
"""

from datetime import date

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.logical import LogicalAccount, load_logical_data
from moneta.models import (
    SPEND_ACCOUNT_TYPES,
    AccountType,
    RecurringSeries,
    SeriesStatus,
    Transaction,
)
from moneta.queries import ClassifiedLink, classified_links, primary_currency


class TxnRow(BaseModel):
    id: int
    posted_on: date
    account: str
    account_type: str
    merchant: str | None
    description: str
    amount_cents: int  # signed
    series: str | None  # owning series' merchant label
    series_status: str | None  # active|ended
    series_discretionary: bool | None
    link: str | None  # None | "internal" | "loan_payment" | "cc_payment"
    counted_in_spend: bool
    excluded_because: str | None  # human-readable single reason when not counted


def link_field(
    txn_id: int, by_outflow: dict[int, ClassifiedLink], inflow_ids: set[int]
) -> str | None:
    outflow_link = by_outflow.get(txn_id)
    if outflow_link is not None:
        if outflow_link.inflow_is_loan_like:
            return "loan_payment"
        if outflow_link.inflow_account_type == AccountType.credit:
            return "cc_payment"
        return "internal"
    if txn_id in inflow_ids:
        return "internal"
    return None


def spend_reason(
    txn_amount_cents: int,
    account_type: str,
    account_loan_like: bool,
    currency: str,
    primary: str,
    link: str | None,
    series_status: str | None,
    series_discretionary: bool | None,
) -> str | None:
    """The counted-in-spend predicate, in spec §2's precedence order — pure, no
    session. `None` means counted; any other value is the (generic) reason it isn't.
    `series_status`/`series_discretionary` describe the txn's owning series, or None
    when it isn't tagged to one. Shared by transactions._excluded_because (which adds
    the series' merchant name to the "fixed cost" reason) and power.spent_so_far, so
    the two views can never quietly diverge on what counts as spend.
    `account_loan_like` is the txn's logical account's `loan_like`: that account's own
    outflows (purchases, interest, fees) are paid off by the payments `power` counts as
    its fixed cost, so counting them as spend too would count them twice."""
    if txn_amount_cents >= 0:
        return "inflow"
    if link == "loan_payment":
        return "loan payment"
    if link == "cc_payment":
        return "credit-card payment"
    if link == "internal":
        return "transfer"
    if series_status == SeriesStatus.active and not series_discretionary:
        return "fixed cost"
    if account_type not in SPEND_ACCOUNT_TYPES:
        return "non-spend account"
    if account_loan_like:
        return "financed purchase"
    if currency != primary:
        return "foreign currency"
    return None


def _excluded_because(
    txn: Transaction,
    account: LogicalAccount,
    series: RecurringSeries | None,
    link: str | None,
    primary: str,
) -> str | None:
    """`_excluded_because` string for a TxnRow. `counted_in_spend` is
    `excluded_because is None` by construction — never a separately maintained bool."""
    reason = spend_reason(
        txn.amount_cents,
        account.type,
        account.loan_like,
        account.currency,
        primary,
        link,
        series.status if series is not None else None,
        series.discretionary if series is not None else None,
    )
    if reason == "fixed cost" and series is not None:
        return f"fixed cost (series {series.merchant})"
    return reason


async def transactions_report(
    session: AsyncSession,
    start: date,
    end: date,
    account_id: int | None = None,
    merchant: str | None = None,
    include_sources: bool = False,
) -> list[TxnRow]:
    primary = await primary_currency(session)
    links = await classified_links(session)
    by_outflow = {link.outflow_id: link for link in links}
    inflow_ids = {link.inflow_id for link in links}

    logical = await load_logical_data(session)
    series_by_id = {s.id: s for s in await session.scalars(select(RecurringSeries))}
    txns = (
        list(await session.scalars(select(Transaction)))
        if include_sources
        else logical.transactions
    )
    account_root = (
        logical.account_roots.get(account_id, account_id) if account_id is not None else None
    )
    rows = sorted(
        (
            t
            for t in txns
            if start <= t.posted_on <= end
            and (account_root is None or logical.account_roots[t.account_id] == account_root)
            and (merchant is None or merchant.casefold() in (t.merchant or "").casefold())
        ),
        key=lambda t: (t.posted_on, t.id),
        reverse=True,
    )
    result: list[TxnRow] = []
    for txn in rows:
        account = logical.accounts[logical.account_roots[txn.account_id]]
        series = series_by_id.get(txn.series_id) if txn.series_id is not None else None
        duplicate = logical.transaction_roots[txn.id] != txn.id
        link = None if duplicate else link_field(txn.id, by_outflow, inflow_ids)
        reason = (
            "provider_duplicate"
            if duplicate
            else _excluded_because(txn, account, series, link, primary)
        )
        result.append(
            TxnRow(
                id=txn.id,
                posted_on=txn.posted_on,
                account=account.name,
                account_type=account.type,
                merchant=txn.merchant,
                description=txn.description,
                amount_cents=txn.amount_cents,
                series=series.merchant if series is not None else None,
                series_status=series.status if series is not None else None,
                series_discretionary=series.discretionary if series is not None else None,
                link=link,
                counted_in_spend=reason is None,
                excluded_because=reason,
            )
        )
    return result


def spend_totals(rows: list[TxnRow], start: date, end: date, today: date) -> tuple[int, int | None]:
    """Envelope totals for a transactions_report result: (counted_total_cents,
    through_today_cents). counted_total sums every counted row's magnitude,
    regardless of date. through_today sums only counted rows dated on or before
    `today`, and is None when [start, end] doesn't cover `today` at all — there's no
    "through today" concept for a range that doesn't include it."""
    counted_total = sum(-r.amount_cents for r in rows if r.counted_in_spend)
    if not (start <= today <= end):
        return counted_total, None
    through_today = sum(
        -r.amount_cents for r in rows if r.counted_in_spend and r.posted_on <= today
    )
    return counted_total, through_today
