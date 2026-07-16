from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import Snapshot
from moneta.models import Account, AccountType, Holding, Transaction, to_cents

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
                type=dto.type_hint or infer_account_type(dto.name, dto.org_name),
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

    seen = {
        (aid, tid)
        for aid, tid in (
            await session.execute(select(Transaction.account_id, Transaction.aggregator_id))
        ).all()
    }
    for txn in snap.transactions:
        if txn.account_id not in acct_ids:
            continue
        key = (acct_ids[txn.account_id], txn.id)
        if key in seen:
            continue
        seen.add(key)
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
        stats.new_transactions += 1

    for h in snap.holdings:
        if h.account_id not in acct_ids:
            continue
        acct_pk = acct_ids[h.account_id]
        row = (
            await session.execute(
                select(Holding).where(Holding.account_id == acct_pk, Holding.symbol == h.symbol)
            )
        ).scalar_one_or_none()
        if row is None:
            row = Holding(
                account_id=acct_pk,
                symbol=h.symbol,
                quantity=h.quantity,
                market_value_cents=to_cents(h.market_value),
            )
            session.add(row)
        else:
            row.quantity = h.quantity
            row.market_value_cents = to_cents(h.market_value)
        stats.holdings += 1

    await session.commit()
    return stats
