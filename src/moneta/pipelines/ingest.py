from datetime import date

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import Snapshot
from moneta.models import Account, AccountType, Holding, Transaction, TransferLink, to_cents

_TYPE_HINTS: list[tuple[AccountType, tuple[str, ...]]] = [
    (AccountType.checking, ("checking",)),
    (AccountType.savings, ("savings", "saving")),
    (AccountType.credit, ("credit", "card")),
    (AccountType.loan, ("loan", "financing", "synchrony", "affirm", "mortgage")),
    (AccountType.brokerage, ("brokerage", "fidelity", "vanguard", "schwab", "individual")),
]


def infer_account_type(name: str, org_name: str) -> AccountType:
    text = f"{name} {org_name}".lower()
    for acct_type, needles in _TYPE_HINTS:
        if any(n in text for n in needles):
            return acct_type
    return AccountType.unknown


class IngestStats(BaseModel):
    new_accounts: int = 0
    new_transactions: int = 0
    updated_transactions: int = 0
    holdings: int = 0


async def ingest_snapshot(session: AsyncSession, snap: Snapshot) -> IngestStats:
    stats = IngestStats()
    acct_ids: dict[str, int] = {}

    for dto in snap.accounts:
        existing = (
            await session.execute(select(Account).where(Account.aggregator_id == dto.id))
        ).scalar_one_or_none()
        if existing is None:
            existing = Account(
                aggregator_id=dto.id,
                name=dto.name,
                org_name=dto.org_name,
                currency=dto.currency,
                type=infer_account_type(dto.name, dto.org_name),
                balance_cents=to_cents(dto.balance),
                balance_date=dto.balance_date,
            )
            session.add(existing)
            await session.flush()
            stats.new_accounts += 1
        else:
            existing.balance_cents = to_cents(dto.balance)
            existing.balance_date = dto.balance_date
        acct_ids[dto.id] = existing.id

    # comparison columns only — materializing every historical row per sync doesn't scale
    existing_txns: dict[tuple[int, str], tuple[int, int, date, str]] = {
        (aid, agg): (tid, cents, posted, desc)
        for tid, aid, agg, cents, posted, desc in (
            await session.execute(
                select(
                    Transaction.id,
                    Transaction.account_id,
                    Transaction.aggregator_id,
                    Transaction.amount_cents,
                    Transaction.posted_on,
                    Transaction.description,
                )
            )
        ).all()
    }
    relinked_ids: set[int] = set()
    snapshot_new: set[tuple[int, str]] = set()
    for txn in snap.transactions:
        if txn.account_id not in acct_ids:
            continue
        key = (acct_ids[txn.account_id], txn.id)
        info = existing_txns.get(key)
        if info is not None:
            # institutions re-post corrections under the same id — take the new values
            tid, old_cents, old_posted, old_desc = info
            fields = (to_cents(txn.amount), txn.posted_on, txn.description)
            if fields == (old_cents, old_posted, old_desc):
                continue
            row = await session.get_one(Transaction, tid)
            if txn.description != old_desc:
                # merchant/series derive from the description — clear so normalize
                # and detection (which run right after ingest) re-derive them
                row.merchant = None
                row.series_id = None
            if (to_cents(txn.amount), txn.posted_on) != (old_cents, old_posted):
                relinked_ids.add(tid)  # amount/date matches behind a TransferLink are void
            row.amount_cents, row.posted_on, row.description = fields
            row.raw = txn.raw
            existing_txns[key] = (tid, *fields)
            stats.updated_transactions += 1
            continue
        if key in snapshot_new:
            continue
        session.add(
            Transaction(
                account_id=key[0],
                aggregator_id=txn.id,
                posted_on=txn.posted_on,
                amount_cents=to_cents(txn.amount),
                description=txn.description,
                raw=txn.raw,
            )
        )
        snapshot_new.add(key)
        stats.new_transactions += 1

    if relinked_ids:
        stale_links = (
            (
                await session.execute(
                    select(TransferLink).where(
                        or_(
                            TransferLink.outflow_id.in_(relinked_ids),
                            TransferLink.inflow_id.in_(relinked_ids),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for link in stale_links:  # link_transfers re-matches on the corrected values
            await session.delete(link)

    for h in snap.holdings:
        if h.account_id not in acct_ids:
            continue
        acct_pk = acct_ids[h.account_id]
        holding = (
            await session.execute(
                select(Holding).where(Holding.account_id == acct_pk, Holding.symbol == h.symbol)
            )
        ).scalar_one_or_none()
        if holding is None:
            session.add(
                Holding(
                    account_id=acct_pk,
                    symbol=h.symbol,
                    quantity=h.quantity,
                    market_value_cents=to_cents(h.market_value),
                )
            )
        else:
            holding.quantity = h.quantity
            holding.market_value_cents = to_cents(h.market_value)
        stats.holdings += 1

    await session.commit()
    return stats
