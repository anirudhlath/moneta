from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, Holding, SyncRun
from moneta.views.networth import net_worth_report
from tests.factories import make_account


async def test_net_worth_counts_only_vested(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.checking, balance_cents=500000)
    await make_account(session, type=AccountType.credit, balance_cents=-120000)
    brokerage = await make_account(session, type=AccountType.brokerage)
    session.add(
        Holding(
            account_id=brokerage.id,
            symbol="ACME",
            quantity=100.0,
            market_value_cents=1000000,
            vested_quantity=40.0,
            unvested_quantity=60.0,
        )
    )
    await session.flush()
    r = await net_worth_report(session)
    assert r.liquid_cents == 500000
    assert r.liabilities_cents == 120000
    assert r.vested_holdings_cents == 400000  # 40/100 of $10,000
    assert r.unvested_potential_cents == 600000
    assert r.net_worth_cents == 780000


async def test_holding_without_vesting_data_counts_fully(session: AsyncSession) -> None:
    brokerage = await make_account(session, type=AccountType.brokerage)
    session.add(
        Holding(account_id=brokerage.id, symbol="VTI", quantity=10.0, market_value_cents=250000)
    )
    await session.flush()
    r = await net_worth_report(session)
    assert r.vested_holdings_cents == 250000 and r.unvested_potential_cents == 0


async def test_unknown_accounts_flagged(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.unknown, balance_cents=99900)
    r = await net_worth_report(session)
    assert r.unknown_accounts == 1 and r.net_worth_cents == 0


async def test_vested_fraction_over_one_is_clamped(session: AsyncSession) -> None:
    brokerage = await make_account(session, type=AccountType.brokerage)
    session.add(
        Holding(
            account_id=brokerage.id,
            symbol="ACME",
            quantity=100.0,
            market_value_cents=1000000,
            vested_quantity=150.0,  # stale import: exceeds total quantity
        )
    )
    await session.flush()
    r = await net_worth_report(session)
    assert r.vested_holdings_cents == 1000000  # clamped to full market value, not 15000
    assert r.net_worth_cents == 1000000


async def test_foreign_currency_accounts_excluded(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.checking, balance_cents=100_000)
    await make_account(session, type=AccountType.checking, balance_cents=55_500, currency="EUR")
    r = await net_worth_report(session)
    assert r.liquid_cents == 100000
    assert r.foreign_accounts == 1


async def test_data_as_of_is_none_with_no_sync_run(session: AsyncSession) -> None:
    r = await net_worth_report(session)
    assert r.data_as_of is None


async def test_data_as_of_reports_newest_successful_run(session: AsyncSession) -> None:
    session.add(SyncRun(success=True, finished_at=datetime(2026, 7, 5, 9, 0)))
    session.add(SyncRun(success=True, finished_at=datetime(2026, 7, 6, 10, 0)))
    session.add(SyncRun(success=False, finished_at=datetime(2026, 7, 7, 11, 0)))
    await session.flush()
    r = await net_worth_report(session)
    assert r.data_as_of == datetime(2026, 7, 6, 10, 0)
