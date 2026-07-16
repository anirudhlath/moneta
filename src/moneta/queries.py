"""Shared query helpers for classifying transfer-link legs by account.

Several views/pipelines need to answer the same question: for each
TransferLink, what kind of account does each leg land in, and (for the
outflow leg) which recurring series and posting date does it carry? This
module answers that once so cashflow/power/recurring/financing don't each
re-scan TransferLink and rebuild their own txn->account/type/series maps.
"""

import statistics
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import match_cadence
from moneta.models import Account, AccountType, Cadence, Transaction, TransferLink


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
    rows = (
        await session.execute(select(Account.currency, func.count()).group_by(Account.currency))
    ).all()
    counts: list[tuple[str, int]] = [(currency, n) for currency, n in rows]
    if not counts:
        return "USD"
    return max(counts, key=lambda r: (r[1], r[0] == "USD", r[0]))[0]


async def account_type_map(session: AsyncSession) -> dict[int, AccountType]:
    rows = (await session.execute(select(Account.id, Account.type))).all()
    return {aid: atype for aid, atype in rows}


async def classified_links(session: AsyncSession) -> list[ClassifiedLink]:
    links = (await session.execute(select(TransferLink))).scalars().all()
    if not links:
        return []
    txn_ids = {link.outflow_id for link in links} | {link.inflow_id for link in links}
    rows = (
        await session.execute(
            select(
                Transaction.id,
                Transaction.account_id,
                Transaction.series_id,
                Transaction.posted_on,
                Transaction.amount_cents,
            ).where(Transaction.id.in_(txn_ids))
        )
    ).all()
    txn_info = {tid: (aid, sid, posted, amt) for tid, aid, sid, posted, amt in rows}
    acct_rows = (
        await session.execute(select(Account.id, Account.type, Account.financing_mode))
    ).all()
    acct_info = {aid: (atype, financing) for aid, atype, financing in acct_rows}

    result: list[ClassifiedLink] = []
    for link in links:
        outflow_info = txn_info.get(link.outflow_id)
        inflow_info = txn_info.get(link.inflow_id)
        if outflow_info is None or inflow_info is None:
            continue  # a leg's txn is missing (shouldn't happen, but don't blow up)
        out_account_id, out_series_id, out_posted_on, out_amount_cents = outflow_info
        in_account_id, _in_series_id, _in_posted_on, _in_amount_cents = inflow_info
        out_type, _out_financing = acct_info.get(out_account_id, (AccountType.unknown, False))
        in_type, in_financing = acct_info.get(in_account_id, (AccountType.unknown, False))
        result.append(
            ClassifiedLink(
                outflow_id=link.outflow_id,
                inflow_id=link.inflow_id,
                outflow_account_id=out_account_id,
                inflow_account_id=in_account_id,
                outflow_account_type=out_type,
                inflow_account_type=in_type,
                outflow_series_id=out_series_id,
                outflow_posted_on=out_posted_on,
                outflow_amount_cents=out_amount_cents,
                inflow_is_loan_like=(in_type == AccountType.loan or in_financing),
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
