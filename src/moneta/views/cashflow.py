from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, Transaction, TransferLink, from_cents

_SPEND_TYPES = (AccountType.checking, AccountType.savings, AccountType.credit)
_LIQUID_TYPES = (AccountType.checking, AccountType.savings)


async def _linked_map(session: AsyncSession) -> dict[int, int]:
    """outflow txn id -> inflow txn id for all transfer links."""
    return {
        link.outflow_id: link.inflow_id
        for link in (await session.execute(select(TransferLink))).scalars()
    }


async def _txn_account_types(session: AsyncSession) -> dict[int, AccountType]:
    rows = (
        await session.execute(
            select(Transaction.id, Account.type).join(Account, Transaction.account_id == Account.id)
        )
    ).all()
    return {tid: atype for tid, atype in rows}


async def accrual_spend(session: AsyncSession, start: date, end: date) -> Decimal:
    linked = await _linked_map(session)
    inflow_ids = set(linked.values())
    txns = (
        (
            await session.execute(
                select(Transaction)
                .join(Account, Transaction.account_id == Account.id)
                .where(
                    Transaction.amount_cents < 0,
                    Transaction.posted_on >= start,
                    Transaction.posted_on <= end,
                    Account.type.in_(_SPEND_TYPES),
                )
            )
        )
        .scalars()
        .all()
    )
    total = sum(-t.amount_cents for t in txns if t.id not in linked and t.id not in inflow_ids)
    return from_cents(total)


async def cash_out(session: AsyncSession, start: date, end: date) -> Decimal:
    linked = await _linked_map(session)
    types = await _txn_account_types(session)
    txns = (
        (
            await session.execute(
                select(Transaction)
                .join(Account, Transaction.account_id == Account.id)
                .where(
                    Transaction.amount_cents < 0,
                    Transaction.posted_on >= start,
                    Transaction.posted_on <= end,
                    Account.type.in_(_LIQUID_TYPES),
                )
            )
        )
        .scalars()
        .all()
    )
    total = 0
    for t in txns:
        inflow_id = linked.get(t.id)
        if inflow_id is not None and types.get(inflow_id) in _LIQUID_TYPES:
            continue  # internal liquid->liquid move
        total += -t.amount_cents
    return from_cents(total)
