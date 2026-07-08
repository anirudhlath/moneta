from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, Holding, from_cents


class NetWorthReport(BaseModel):
    liquid: Decimal
    vested_holdings: Decimal
    liabilities: Decimal
    net_worth: Decimal
    unvested_potential: Decimal
    unknown_accounts: int


async def net_worth_report(session: AsyncSession) -> NetWorthReport:
    accounts = (await session.execute(select(Account))).scalars().all()
    liquid = sum(
        a.balance_cents for a in accounts if a.type in (AccountType.checking, AccountType.savings)
    )
    liabilities = sum(
        abs(a.balance_cents) for a in accounts if a.type in (AccountType.credit, AccountType.loan)
    )
    unknown = sum(1 for a in accounts if a.type == AccountType.unknown)

    vested_cents = 0
    unvested_cents = 0
    for h in (await session.execute(select(Holding))).scalars():
        if h.vested_quantity is None or h.quantity <= 0:
            vested_cents += h.market_value_cents
            continue
        vested_frac = h.vested_quantity / h.quantity
        unvested_frac = (h.unvested_quantity or 0.0) / h.quantity
        vested_cents += round(h.market_value_cents * vested_frac)
        unvested_cents += round(h.market_value_cents * unvested_frac)

    return NetWorthReport(
        liquid=from_cents(liquid),
        vested_holdings=from_cents(vested_cents),
        liabilities=from_cents(liabilities),
        net_worth=from_cents(liquid + vested_cents - liabilities),
        unvested_potential=from_cents(unvested_cents),
        unknown_accounts=unknown,
    )
