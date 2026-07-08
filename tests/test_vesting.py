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
