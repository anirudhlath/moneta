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
from datetime import date, datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import match_cadence
from moneta.logical import load_logical_data
from moneta.models import AccountType, Cadence, SyncRun, TransferLink


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


async def latest_successful_sync_at(session: AsyncSession) -> datetime | None:
    """Newest successful SyncRun.finished_at — the staleness clock power/networth
    report as `data_as_of`. A failed run may have rolled back mid-sync, so only a
    success reflects what the current tables actually show."""
    return (
        await session.execute(
            select(func.max(SyncRun.finished_at)).where(SyncRun.success.is_(True))
        )
    ).scalar_one_or_none()


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
                inflow_is_loan_like=in_account.type == AccountType.loan
                or in_account.financing_mode,
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


def loan_payment_stats(links: list[ClassifiedLink]) -> dict[int, LoanPayment]:
    """Per-loan-account payment cadence/amount derived from transfer-linked outflows.

    Banks collapse different loans' payments into one descriptor; the link's inflow
    account is the reliable per-loan identity (design 2026-07-16 §3).
    """
    by_account: dict[int, list[ClassifiedLink]] = {}
    for link in links:
        if link.inflow_is_loan_like:
            by_account.setdefault(link.inflow_account_id, []).append(link)
    result: dict[int, LoanPayment] = {}
    for account_id, group in by_account.items():
        dates = sorted({link.outflow_posted_on for link in group})
        match = match_cadence(dates)
        if match is None:
            cadence, run_start = Cadence.monthly, dates[0]  # loans are near-universally monthly
        else:
            cadence, run_start = match
        amounts = [
            abs(link.outflow_amount_cents) for link in group if link.outflow_posted_on >= run_start
        ]
        result[account_id] = LoanPayment(
            account_id=account_id,
            cadence=cadence,
            expected_cents=-round(statistics.median(amounts)),
            last_paid_on=dates[-1],
        )
    return result
