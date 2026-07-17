import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, Holding
from moneta.vesting import apply_vesting, parse_vesting_csv
from tests.factories import make_account

CSV = """symbol,vested_quantity,unvested_quantity
ACME,40,60
VTI,10,0
"""


def test_parse_vesting_csv() -> None:
    rows = parse_vesting_csv(CSV)
    assert len(rows) == 2
    assert rows[0].symbol == "ACME"
    assert rows[0].vested_quantity == 40.0 and rows[0].unvested_quantity == 60.0


def test_parse_rejects_bad_header() -> None:
    with pytest.raises(ValueError, match="expected header"):
        parse_vesting_csv("ticker,vested\nACME,40\n")


def test_parse_rejects_short_row() -> None:
    with pytest.raises(ValueError):
        parse_vesting_csv("symbol,vested_quantity,unvested_quantity\nACME,40\n")


async def test_apply_vesting_updates_holdings(session: AsyncSession) -> None:
    brokerage = await make_account(session, type=AccountType.brokerage)
    session.add(
        Holding(account_id=brokerage.id, symbol="ACME", quantity=100.0, market_value_cents=1000000)
    )
    await session.flush()
    n = await apply_vesting(session, parse_vesting_csv(CSV))
    assert n == 1  # VTI row has no matching holding
    h = (await session.execute(select(Holding))).scalar_one()
    assert h.vested_quantity == 40.0 and h.unvested_quantity == 60.0


async def test_apply_vesting_updates_every_holding_sharing_symbol_across_accounts(
    session: AsyncSession,
) -> None:
    """apply_vesting matches by symbol alone (no account scoping) — the same symbol
    held in two different accounts (e.g. two brokerage sub-accounts) both update."""
    acct1 = await make_account(session, type=AccountType.brokerage)
    acct2 = await make_account(session, type=AccountType.brokerage)
    session.add(
        Holding(account_id=acct1.id, symbol="ACME", quantity=50.0, market_value_cents=500000)
    )
    session.add(
        Holding(account_id=acct2.id, symbol="ACME", quantity=25.0, market_value_cents=250000)
    )
    await session.flush()
    n = await apply_vesting(session, parse_vesting_csv(CSV))
    assert n == 2
    holdings = (await session.execute(select(Holding))).scalars().all()
    assert len(holdings) == 2
    assert all(h.vested_quantity == 40.0 and h.unvested_quantity == 60.0 for h in holdings)


async def test_apply_vesting_duplicate_symbol_rows_last_row_wins(session: AsyncSession) -> None:
    """CSV with the same symbol on two rows: apply_vesting processes rows in file
    order and each row overwrites every matching holding, so the last row's values
    are what a matching holding ends up with (documented actual behavior, not a
    dedup/merge)."""
    brokerage = await make_account(session, type=AccountType.brokerage)
    session.add(
        Holding(account_id=brokerage.id, symbol="ACME", quantity=100.0, market_value_cents=1000000)
    )
    await session.flush()
    csv_text = "symbol,vested_quantity,unvested_quantity\nACME,10,90\nACME,40,60\n"
    n = await apply_vesting(session, parse_vesting_csv(csv_text))
    assert n == 2  # both rows matched and updated the one holding
    h = (await session.execute(select(Holding))).scalar_one()
    assert h.vested_quantity == 40.0 and h.unvested_quantity == 60.0  # last row wins
