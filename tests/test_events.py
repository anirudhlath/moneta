from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Cadence, Direction, EventKind, RecurringSeries, SeriesEvent
from moneta.pipelines.events import emit_series_events
from tests.factories import make_account, make_txn


async def _mk_series(session: AsyncSession, **kw: object) -> RecurringSeries:
    defaults: dict[str, object] = {
        "merchant": "Netflix",
        "direction": Direction.outflow,
        "cadence": Cadence.monthly,
        "expected_cents": -1599,
        "next_expected_on": date(2026, 6, 15),
    }
    s = RecurringSeries(**{**defaults, **kw})
    session.add(s)
    await session.flush()
    return s


async def test_missed_payment_emits_once_and_advances(session: AsyncSession) -> None:
    s = await _mk_series(session)  # expected 6/15, grace 7 → missed after 6/22
    n = await emit_series_events(session, today=date(2026, 7, 1))
    assert n == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.missed and ev.series_id == s.id
    assert s.next_expected_on == date(2026, 7, 15)
    assert await emit_series_events(session, today=date(2026, 7, 1)) == 0  # no re-fire


async def test_payment_on_time_no_miss(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await _mk_series(session)
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
    s = await _mk_series(session, next_expected_on=date(2026, 8, 15))
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


async def test_small_variation_not_price_increase(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await _mk_series(session, next_expected_on=date(2026, 8, 15))
    await make_txn(
        session,
        acct,
        amount_cents=-1620,
        merchant="Netflix",
        posted_on=date(2026, 7, 15),
        series_id=s.id,
    )  # +1.3%
    assert await emit_series_events(session, today=date(2026, 7, 16)) == 0
