from datetime import date

from pydantic import BaseModel
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import Snapshot
from moneta.models import (
    Account,
    AccountType,
    Holding,
    RecurringSeries,
    SeriesStatus,
    Transaction,
    TransferLink,
    to_cents,
)

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
    deleted_transactions: int = 0


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
                type=dto.type_hint or infer_account_type(dto.name, dto.org_name),
                balance_cents=to_cents(dto.balance),
                balance_date=dto.balance_date,
                source=dto.source,
                mask=dto.mask,
                raw=dto.raw,
            )
            session.add(existing)
            await session.flush()
            stats.new_accounts += 1
        else:
            existing.balance_cents = to_cents(dto.balance)
            existing.balance_date = dto.balance_date
            existing.source = dto.source  # natural backfill for pre-attribution accounts
            existing.mask = dto.mask
            existing.raw = dto.raw
        acct_ids[dto.id] = existing.id

    # comparison columns as plain tuples — ORM rows are only fetched for the rare correction
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
                # the merchant derives from the description — clear it so normalize
                # (which runs right after ingest) re-derives it. series_id stays:
                # detection untags only if the re-derived merchant actually moved,
                # so a same-merchant correction can't fake a "genuinely new" txn
                # and revive an ended series
                row.merchant = None
            if fields[:2] != (old_cents, old_posted):
                relinked_ids.add(tid)  # amount/date matches behind overlap/transfer links are void
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

    # link_transfers and overlap reconciliation re-match on the corrected values
    await _void_relations(session, relinked_ids)
    gone = _absent_from_replay(snap, acct_ids, existing_txns)
    if gone:
        await _delete_absent(session, gone)
        stats.deleted_transactions = len(gone)

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


async def _void_relations(session: AsyncSession, ids: set[int]) -> None:
    """Clear the duplicate pointers and transfer links resting on `ids`.

    Provider-overlap reconciliation and link_transfers, which run right after
    ingest, re-match from whatever remains.
    """
    if not ids:
        return
    await session.execute(
        update(Transaction)
        .where(or_(Transaction.id.in_(ids), Transaction.duplicate_of_id.in_(ids)))
        .values(duplicate_of_id=None)
    )
    stale_links = await session.scalars(
        select(TransferLink).where(
            or_(TransferLink.outflow_id.in_(ids), TransferLink.inflow_id.in_(ids))
        )
    )
    for link in stale_links.all():
        await session.delete(link)


def _absent_from_replay(
    snap: Snapshot,
    acct_ids: dict[str, int],
    stored: dict[tuple[int, str], tuple[int, int, date, str]],
) -> set[int]:
    """Ids of stored rows a complete Plaid replay should have returned but did not.

    Only Plaid accounts the snapshot gives a replay floor are judged, and only their
    rows dated on or after that floor: older rows have aged out of the replay window,
    not been withdrawn.
    """
    plaid = {dto.id for dto in snap.accounts if dto.source == "plaid"}
    floors = {
        acct_ids[ext]: floor
        for ext, floor in snap.complete_replays.items()
        if ext in plaid and ext in acct_ids
    }
    if not floors:
        return set()
    returned = {
        (acct_ids[t.account_id], t.id) for t in snap.transactions if t.account_id in acct_ids
    }
    return {
        tid
        for key, (tid, _cents, posted, _desc) in stored.items()
        if key[0] in floors and posted >= floors[key[0]] and key not in returned
    }


async def _delete_absent(session: AsyncSession, gone: set[int]) -> None:
    """Delete withdrawn rows, leaving nothing pointing at them."""
    tags: dict[int, int | None] = {
        tid: sid
        for tid, sid in (
            await session.execute(
                select(Transaction.id, Transaction.series_id).where(Transaction.id.in_(gone))
            )
        ).all()
    }
    survivors = await session.scalars(
        select(Transaction).where(
            Transaction.duplicate_of_id.in_(gone), Transaction.id.not_in(gone)
        )
    )
    for survivor in survivors.all():
        # the other provider's copy becomes the counted row; carrying the bill's tag
        # keeps detection from mistaking it for a genuinely new, untagged charge
        if survivor.series_id is None and survivor.duplicate_of_id is not None:
            survivor.series_id = tags.get(survivor.duplicate_of_id)
    await _void_relations(session, gone)
    lost = {sid for sid in tags.values() if sid is not None}
    if lost:
        kept = set(
            await session.scalars(
                select(Transaction.series_id).where(
                    Transaction.series_id.in_(lost), Transaction.id.not_in(gone)
                )
            )
        )
        if orphaned := lost - kept:
            # with no tagged row left the series would keep firing `missed` events;
            # detection revives it if a genuinely new charge arrives
            await session.execute(
                update(RecurringSeries)
                .where(
                    RecurringSeries.id.in_(orphaned),
                    RecurringSeries.status == SeriesStatus.active,
                )
                .values(status=SeriesStatus.ended)
            )
    # transactions is AUTOINCREMENT (Task 4): these ids are never handed out again
    await session.execute(delete(Transaction).where(Transaction.id.in_(gone)))
