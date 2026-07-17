from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    LIABILITY_ACCOUNT_TYPES,
    LIQUID_ACCOUNT_TYPES,
    Account,
    AccountType,
    Holding,
)
from moneta.queries import latest_successful_sync_at, primary_currency


class NetWorthReport(BaseModel):
    liquid_cents: int
    vested_holdings_cents: int
    liabilities_cents: int
    net_worth_cents: int
    unvested_potential_cents: int
    unknown_accounts: int
    foreign_accounts: int
    data_as_of: datetime | None  # newest successful SyncRun.finished_at (design 2026-07-16 §3)


async def net_worth_report(session: AsyncSession) -> NetWorthReport:
    primary = await primary_currency(session)
    accounts = (await session.execute(select(Account))).scalars().all()
    domestic = [a for a in accounts if a.currency == primary]
    liquid = sum(a.balance_cents for a in domestic if a.type in LIQUID_ACCOUNT_TYPES)
    liabilities = sum(abs(a.balance_cents) for a in domestic if a.type in LIABILITY_ACCOUNT_TYPES)
    unknown = sum(1 for a in domestic if a.type == AccountType.unknown)

    vested_cents = 0
    unvested_cents = 0
    holdings = (
        (
            await session.execute(
                select(Holding)
                .join(Account, Holding.account_id == Account.id)
                .where(Account.currency == primary)
            )
        )
        .scalars()
        .all()
    )
    for h in holdings:
        if h.vested_quantity is None or h.quantity <= 0:
            vested_cents += h.market_value_cents
            continue
        vested_frac = min(max(h.vested_quantity / h.quantity, 0.0), 1.0)
        unvested_frac = min(max((h.unvested_quantity or 0.0) / h.quantity, 0.0), 1.0)
        vested_cents += round(h.market_value_cents * vested_frac)
        unvested_cents += round(h.market_value_cents * unvested_frac)

    return NetWorthReport(
        liquid_cents=liquid,
        vested_holdings_cents=vested_cents,
        liabilities_cents=liabilities,
        net_worth_cents=liquid + vested_cents - liabilities,
        unvested_potential_cents=unvested_cents,
        unknown_accounts=unknown,
        foreign_accounts=len(accounts) - len(domestic),
        data_as_of=await latest_successful_sync_at(session),
    )
