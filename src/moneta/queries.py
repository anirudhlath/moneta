"""Shared query helpers for classifying transfer-link legs by account.

Several views/pipelines need to answer the same question: for each
TransferLink, what kind of account does each leg land in, and (for the
outflow leg) which recurring series and posting date does it carry? This
module answers that once so cashflow/power/recurring/financing don't each
re-scan TransferLink and rebuild their own txn->account/type/series maps.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, Transaction, TransferLink


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
            ).where(Transaction.id.in_(txn_ids))
        )
    ).all()
    txn_info = {tid: (aid, sid, posted) for tid, aid, sid, posted in rows}
    acct_types = await account_type_map(session)

    result: list[ClassifiedLink] = []
    for link in links:
        outflow_info = txn_info.get(link.outflow_id)
        inflow_info = txn_info.get(link.inflow_id)
        if outflow_info is None or inflow_info is None:
            continue  # a leg's txn is missing (shouldn't happen, but don't blow up)
        out_account_id, out_series_id, out_posted_on = outflow_info
        in_account_id, _in_series_id, _in_posted_on = inflow_info
        result.append(
            ClassifiedLink(
                outflow_id=link.outflow_id,
                inflow_id=link.inflow_id,
                outflow_account_id=out_account_id,
                inflow_account_id=in_account_id,
                outflow_account_type=acct_types.get(out_account_id, AccountType.unknown),
                inflow_account_type=acct_types.get(in_account_id, AccountType.unknown),
                outflow_series_id=out_series_id,
                outflow_posted_on=out_posted_on,
            )
        )
    return result


def linked_txn_ids(links: list[ClassifiedLink]) -> set[int]:
    ids: set[int] = set()
    for link in links:
        ids.update((link.outflow_id, link.inflow_id))
    return ids
