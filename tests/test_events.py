from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import EventKind, SeriesEvent
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.recurring import detect_recurring
from tests.factories import make_account, make_series, make_txn


async def test_missed_payment_emits_once_and_advances(session: AsyncSession) -> None:
    s = await make_series(
        session, next_expected_on=date(2026, 6, 15)
    )  # expected 6/15, grace 7 → missed after 6/22
    n = await emit_series_events(session, today=date(2026, 7, 1))
    assert n == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.missed and ev.series_id == s.id
    assert s.next_expected_on == date(2026, 7, 15)
    assert await emit_series_events(session, today=date(2026, 7, 1)) == 0  # no re-fire


async def test_payment_on_time_no_miss(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 6, 15))
    await make_txn(
        session,
        acct,
        amount_cents=-1599,
        merchant="Netflix",
        posted_on=date(2026, 6, 16),
        series_id=s.id,
    )
    assert await emit_series_events(session, today=date(2026, 7, 1)) == 0


async def test_price_increase_detected(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    await make_txn(
        session,
        acct,
        amount_cents=-1899,
        merchant="Netflix",
        posted_on=date(2026, 7, 15),
        series_id=s.id,
    )
    n = await emit_series_events(session, today=date(2026, 7, 16))
    assert n == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.price_increase
    assert ev.details == {"old_cents": -1599, "new_cents": -1899}
    assert s.expected_cents == -1899
    assert await emit_series_events(session, today=date(2026, 7, 16)) == 0


async def test_auto_ended_series_emits_no_missed_events(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2025, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert await emit_series_events(session, today=date(2026, 7, 8)) == 0
    missed = (
        (await session.execute(select(SeriesEvent).where(SeriesEvent.kind == EventKind.missed)))
        .scalars()
        .all()
    )
    assert missed == []


async def test_small_variation_not_price_increase(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    await make_txn(
        session,
        acct,
        amount_cents=-1620,
        merchant="Netflix",
        posted_on=date(2026, 7, 15),
        series_id=s.id,
    )  # +1.3%
    assert await emit_series_events(session, today=date(2026, 7, 16)) == 0
