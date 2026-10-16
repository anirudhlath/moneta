"""Shared query helpers for classifying transfer-link legs by account.

Several views/pipelines need to answer the same question: for each
TransferLink, what kind of account does each leg land in, and (for the
outflow leg) which recurring series and posting date does it carry? This
module answers that once so cashflow/power/recurring/financing don't each
re-scan TransferLink and rebuild their own txn->account/type/series maps.
"""

import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import match_cadence
from moneta.logical import LogicalData, load_logical_data
from moneta.models import (
    AccountType,
    Cadence,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    SyncRun,
    Transaction,
    TransferLink,
    bill_ref,
)


@dataclass(frozen=True)
class ClassifiedLink:
    outflow_id: int
    inflow_id: int
    outflow_account_id: int
    inflow_account_id: int
    outflow_account_type: AccountType
    inflow_account_type: AccountType
    outflow_series_id: int | None
    outflow_posted_on: date
    outflow_amount_cents: int
    inflow_is_loan_like: bool


async def primary_currency(session: AsyncSession) -> str:
    """Majority currency across accounts; ties prefer USD. Views aggregate only this —
    summing cents across currencies would silently produce a meaningless number."""
    logical = await load_logical_data(session)
    counts = Counter(account.currency for account in logical.accounts.values())
    return (
        max(counts, key=lambda currency: (counts[currency], currency == "USD", currency))
        if counts
        else "USD"
    )


def foreign_series_ids(logical: LogicalData, primary: str) -> set[int]:
    """Tagged survivors identify the denomination of derived series amounts.

    Untagged legacy/manual series retain their established behavior. Mixed tagged
    currencies are ineligible until detection rebuilds the series for one currency.
    """
    currencies: dict[int, set[str]] = {}
    for txn in logical.transactions:
        if txn.series_id is not None:
            currencies.setdefault(txn.series_id, set()).add(
                logical.accounts[logical.account_roots[txn.account_id]].currency
            )
    return {sid for sid, values in currencies.items() if values != {primary}}


def price_review_matches_currency(item: ReviewItem, logical: LogicalData, primary: str) -> bool:
    """Validate new evidence and infer legacy requests from their original occurrence."""
    currency = item.payload.get("currency")
    if currency is not None and currency != primary:
        return False
    if item.payload.get("series_id") in foreign_series_ids(logical, primary):
        return False
    transaction_id = item.payload.get("transaction_id")
    if type(transaction_id) is int:
        survivor = logical.transaction_roots.get(transaction_id)
        matching = [t for t in logical.transactions if t.id == survivor]
        if not matching:
            return False
    else:
        matching = [
            t
            for t in logical.transactions
            if t.merchant == item.payload.get("merchant")
            and t.amount_cents == item.payload.get("new_cents")
            and t.posted_on.isoformat() == item.payload.get("occurred_on")
        ]
    currencies = {logical.accounts[logical.account_roots[t.account_id]].currency for t in matching}
    # A legacy request without surviving evidence keeps its prior behavior, but
    # contradictory or ambiguous surviving currency evidence must never reprice it.
    return not currencies or currencies == {primary}


async def latest_successful_sync_at(session: AsyncSession) -> datetime | None:
    """Newest successful SyncRun.finished_at — the staleness clock power/networth
    report as `data_as_of`. A failed run may have rolled back mid-sync, so only a
    success reflects what the current tables actually show."""
    return (
        await session.execute(
            select(func.max(SyncRun.finished_at)).where(SyncRun.success.is_(True))
        )
    ).scalar_one_or_none()


# Coverage rule (stale feeds): a logical account is flagged once its newest txn is at
# least STALE_MIN_DAYS old AND its usual rate — distinct posting days over the trailing
# COVERAGE_WINDOW_DAYS — predicts at least STALE_EXPECTED_POSTING_DAYS posting days since.
COVERAGE_WINDOW_DAYS = 365
STALE_MIN_DAYS = 7
STALE_EXPECTED_POSTING_DAYS = 5


class StaleAccount(BaseModel):
    """A logical account whose feed went quiet for longer than its usual posting rate
    explains — a coverage note, not an error. `note` is the full user-facing sentence."""

    account_id: int
    name: str
    mask: str | None
    newest_posted_on: date
    posting_days: int  # distinct posting days in the trailing COVERAGE_WINDOW_DAYS
    note: str


def feed_is_stale(posting_days: int, days_since: int) -> bool:
    """`posting_days / 365 * days_since >= 5`, in integers so the boundary is exact."""
    return (
        days_since >= STALE_MIN_DAYS
        and posting_days * days_since >= STALE_EXPECTED_POSTING_DAYS * COVERAGE_WINDOW_DAYS
    )


def _posting_rate_phrase(posting_days: int) -> str:
    # integer posting_days can never land on a .5 here, so round() has no ties to break
    per_week = round(posting_days * 7 / COVERAGE_WINDOW_DAYS)
    if per_week >= 1:
        return f"~{per_week} day{'' if per_week == 1 else 's'} a week"
    per_month = max(1, round(posting_days * 30 / COVERAGE_WINDOW_DAYS))
    return f"~{per_month} day{'' if per_month == 1 else 's'} a month"


async def stale_accounts(
    session: AsyncSession, logical: LogicalData, today: date
) -> list[StaleAccount]:
    """Logical accounts whose feed looks stalled, each judged on its own history.

    Newest is the newest *physical* observation in the logical account — suppressed
    duplicates included — so an account fresh via either provider is fresh. Posting
    days count survivors only, so one purchase seen by both providers days apart is one
    posting day. Accounts without a transaction in the window are skipped.
    """
    newest: dict[int, date] = {}
    rows = await session.execute(
        select(Transaction.account_id, func.max(Transaction.posted_on)).group_by(
            Transaction.account_id
        )
    )
    for account_id, posted_on in rows.tuples():
        root = logical.account_roots[account_id]
        newest[root] = max(newest.get(root, posted_on), posted_on)
    window_start = today - timedelta(days=COVERAGE_WINDOW_DAYS)
    posting: dict[int, set[date]] = {}
    for txn in logical.transactions:
        if window_start < txn.posted_on <= today:
            posting.setdefault(logical.account_roots[txn.account_id], set()).add(txn.posted_on)
    stale: list[StaleAccount] = []
    for root_id, days in sorted(posting.items()):
        last = newest[root_id]
        if not feed_is_stale(len(days), (today - last).days):
            continue
        account = logical.accounts[root_id]
        label = f"{account.name} ··{account.mask}" if account.mask else account.name
        stale.append(
            StaleAccount(
                account_id=root_id,
                name=account.name,
                mask=account.mask,
                newest_posted_on=last,
                posting_days=len(days),
                note=(
                    f"{label} has no transactions since {last.isoformat()} "
                    f"(usually posts {_posting_rate_phrase(len(days))}) — "
                    "recent spending on it may be missing."
                ),
            )
        )
    return stale


async def account_type_map(session: AsyncSession) -> dict[int, AccountType]:
    logical = await load_logical_data(session)
    return {aid: logical.accounts[root].type for aid, root in logical.account_roots.items()}


async def classified_links(session: AsyncSession) -> list[ClassifiedLink]:
    logical = await load_logical_data(session)
    transactions = {txn.id: txn for txn in logical.transactions}
    links = list(await session.scalars(select(TransferLink).order_by(TransferLink.id)))
    result: list[ClassifiedLink] = []
    used: set[int] = set()
    for link in links:
        out = transactions.get(logical.transaction_roots.get(link.outflow_id, -1))
        inn = transactions.get(logical.transaction_roots.get(link.inflow_id, -1))
        if out is None or inn is None or out.id in used or inn.id in used:
            continue
        out_account = logical.accounts[logical.account_roots[out.account_id]]
        in_account = logical.accounts[logical.account_roots[inn.account_id]]
        if out_account.id == in_account.id or out_account.currency != in_account.currency:
            continue
        used.update((out.id, inn.id))
        result.append(
            ClassifiedLink(
                outflow_id=out.id,
                inflow_id=inn.id,
                outflow_account_id=out_account.id,
                inflow_account_id=in_account.id,
                outflow_account_type=out_account.type,
                inflow_account_type=in_account.type,
                outflow_series_id=out.series_id,
                outflow_posted_on=out.posted_on,
                outflow_amount_cents=out.amount_cents,
                inflow_is_loan_like=in_account.loan_like,
            )
        )
    return result


def linked_txn_ids(links: list[ClassifiedLink]) -> set[int]:
    ids: set[int] = set()
    for link in links:
        ids.update((link.outflow_id, link.inflow_id))
    return ids


class LoanPayment(BaseModel):
    account_id: int
    cadence: Cadence
    expected_cents: int  # negative: outflow convention
    last_paid_on: date


# a payment above this multiple of the payments before it is a one-off extra, not the
# new obligation
_ONE_OFF_FACTOR = 1.5
# ...and so is one below this fraction of them: a small extra, not a plan winding down
_ONE_OFF_FLOOR = 0.5
_PRIOR_PAYMENTS = 3  # the yardstick: up to this many payments before the newest


def _current_payment(amounts: list[int]) -> int:
    """What the account is paid now, from its in-run payment magnitudes (oldest first).

    The newest payment, so a plan winding down is not held at its old median — unless it
    is more than _ONE_OFF_FACTOR x or less than _ONE_OFF_FLOOR x the median of the up to
    _PRIOR_PAYMENTS payments before it, in which case that median (exactly 1.5x or exactly
    half keeps the newest). A first payment has nothing before it, so it stands alone.
    """
    newest = amounts[-1]
    prior = amounts[-1 - _PRIOR_PAYMENTS : -1]
    if prior:
        typical = statistics.median(prior)
        if newest > _ONE_OFF_FACTOR * typical or newest < _ONE_OFF_FLOOR * typical:
            return round(typical)
    return newest


def loan_payment_stats(links: list[ClassifiedLink]) -> dict[int, LoanPayment]:
    """Per-loan-account payment cadence/amount derived from transfer-linked outflows.

    Banks collapse different loans' payments into one descriptor; the link's inflow
    account is the reliable per-loan identity (design 2026-07-16 §3). Payments on one
    day are one payment (a payment split in two), so cadence and amount read the same
    per-day totals.
    """
    by_account: dict[int, dict[date, int]] = {}
    for link in links:
        if link.inflow_is_loan_like:
            days = by_account.setdefault(link.inflow_account_id, {})
            day = link.outflow_posted_on
            days[day] = days.get(day, 0) + abs(link.outflow_amount_cents)
    result: dict[int, LoanPayment] = {}
    for account_id, days in by_account.items():
        dates = sorted(days)
        match = match_cadence(dates)
        if match is None:
            cadence, run_start = Cadence.monthly, dates[0]  # loans are near-universally monthly
        else:
            cadence, run_start = match
        result[account_id] = LoanPayment(
            account_id=account_id,
            cadence=cadence,
            expected_cents=-_current_payment([days[d] for d in dates if d >= run_start]),
            last_paid_on=dates[-1],
        )
    return result


async def bill_amount(session: AsyncSession, series: RecurringSeries) -> int | None:
    """The amount a decision about this series names — only when its merchant has
    several bills (design 2026-10-13 §4): more than one series, or an existing
    recurring_cluster question already naming an amount for that merchant and direction
    (a split merchant can have one series plus a question about its other bill).
    Otherwise decisions stay merchant-level."""
    count = await session.scalar(
        select(func.count())
        .select_from(RecurringSeries)
        .where(
            RecurringSeries.merchant == series.merchant,
            RecurringSeries.direction == series.direction,
        )
    )
    if (count or 0) > 1:
        return abs(series.expected_cents)
    for item in await session.scalars(
        select(ReviewItem).where(
            ReviewItem.kind == ReviewKind.recurring_cluster,
            ReviewItem.payload["merchant"].as_string() == series.merchant,
            ReviewItem.payload["direction"].as_string() == series.direction,
        )
    ):
        ref = bill_ref(item.payload)
        if ref is not None and ref[2] is not None:
            return abs(series.expected_cents)
    return None
