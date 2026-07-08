from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    AccountType,
    Cadence,
    Direction,
    RecurringSeries,
    SeriesEvent,
    Transaction,
    TransferLink,
)
from moneta.pipelines.recurring import detect_recurring, monthly_cents
from tests.factories import make_account, make_txn


async def _series(session: AsyncSession) -> list[RecurringSeries]:
    return list((await session.execute(select(RecurringSeries))).scalars().all())


async def test_monthly_subscription_detected(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, month, 15)
        )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.merchant == "Netflix" and s.cadence == Cadence.monthly
    assert s.direction == Direction.outflow and s.expected_cents == -1599
    assert s.next_expected_on == date(2026, 7, 15)
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert all(t.series_id == s.id for t in txns)
    events = (await session.execute(select(SeriesEvent))).scalars().all()
    assert [e.kind for e in events] == ["new_series"]


async def test_biweekly_paycheck_detected_as_inflow(session: AsyncSession) -> None:
    acct = await make_account(session)
    start = date(2026, 5, 1)
    for i in range(4):
        await make_txn(
            session,
            acct,
            amount_cents=250000,
            merchant="Acme Payroll",
            posted_on=start + timedelta(days=14 * i),
        )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.biweekly and s.direction == Direction.inflow


async def test_irregular_amounts_go_to_review(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 0 and stats.review == 1


async def test_non_recurring_ignored(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(
        session, acct, amount_cents=-5000, merchant="One Off", posted_on=date(2026, 6, 1)
    )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 0 and stats.review == 0


async def test_internal_transfers_excluded_loan_payments_kept(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    loan = await make_account(session, type=AccountType.loan)
    for month in (4, 5, 6):
        # internal move checking->savings: excluded
        out_s = await make_txn(
            session,
            checking,
            amount_cents=-10000,
            merchant="Savings Transfer",
            posted_on=date(2026, month, 1),
        )
        in_s = await make_txn(
            session,
            savings,
            amount_cents=10000,
            merchant="Savings Transfer",
            posted_on=date(2026, month, 1),
        )
        session.add(
            TransferLink(outflow_id=out_s.id, inflow_id=in_s.id, confidence=1.0, method="rule")
        )
        # loan payment checking->loan: kept as a series
        out_l = await make_txn(
            session,
            checking,
            amount_cents=-13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
        )
        in_l = await make_txn(
            session,
            loan,
            amount_cents=13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
        )
        session.add(
            TransferLink(outflow_id=out_l.id, inflow_id=in_l.id, confidence=1.0, method="rule")
        )
    await session.flush()
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.merchant == "Synchrony Bank" and s.expected_cents == -13500


async def test_rerun_updates_not_duplicates(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, month, 15)
        )
    await detect_recurring(session, llm=None)
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 7, 15)
    )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 0 and stats.updated == 1
    s = (await _series(session))[0]
    assert s.next_expected_on == date(2026, 8, 14)  # 7/15 + 30 days


def test_monthly_cents_normalization() -> None:
    s = RecurringSeries(
        merchant="X",
        direction=Direction.outflow,
        cadence=Cadence.weekly,
        expected_cents=-1000,
        next_expected_on=date(2026, 7, 1),
    )
    assert monthly_cents(s) == -4333  # -1000 * 52 / 12
    s.cadence = Cadence.annual
    s.expected_cents = -12000
    assert monthly_cents(s) == -1000
