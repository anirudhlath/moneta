from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import EventKind, ReviewItem, ReviewKind, ReviewStatus, SeriesEvent
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.recurring import detect_recurring
from tests.factories import make_account, make_price_change_item, make_series, make_txn


async def test_missed_payment_emits_once_and_advances(session: AsyncSession) -> None:
    s = await make_series(
        session, next_expected_on=date(2026, 6, 15)
    )  # expected 6/15, grace 7 → missed after 6/22
    n = await emit_series_events(session, llm=None, today=date(2026, 7, 1))
    assert n == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.missed and ev.series_id == s.id
    assert s.next_expected_on == date(2026, 7, 15)
    assert await emit_series_events(session, llm=None, today=date(2026, 7, 1)) == 0  # no re-fire


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
    assert await emit_series_events(session, llm=None, today=date(2026, 7, 1)) == 0


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
    n = await emit_series_events(session, llm=None, today=date(2026, 7, 16))
    assert n == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.price_increase
    assert ev.details == {"old_cents": -1599, "new_cents": -1899}
    assert s.expected_cents == -1899
    assert await emit_series_events(session, llm=None, today=date(2026, 7, 16)) == 0


async def test_auto_ended_series_emits_no_missed_events(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2025, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert await emit_series_events(session, llm=None, today=date(2026, 7, 8)) == 0
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
    assert await emit_series_events(session, llm=None, today=date(2026, 7, 16)) == 0


class PriceLLM:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        self.prompts.append(prompt)
        return self.answer


async def _drifted_series(session: AsyncSession) -> tuple[Any, Any]:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    txn = await make_txn(
        session,
        acct,
        amount_cents=-1899,
        merchant="Netflix",
        posted_on=date(2026, 7, 15),
        series_id=s.id,
    )
    return s, txn


async def test_price_change_confident_yes_applies(session: AsyncSession) -> None:
    s, _ = await _drifted_series(session)
    llm = PriceLLM({"is_price_change": True, "confident": True})
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.price_increase
    assert s.expected_cents == -1899


async def test_price_change_unconfident_queues_item_once(session: AsyncSession) -> None:
    s, _ = await _drifted_series(session)
    llm = PriceLLM({"is_price_change": True, "confident": False})
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 0
    assert s.expected_cents == -1599  # not applied
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == ReviewKind.price_change and item.status == ReviewStatus.open
    assert item.payload == {
        "series_id": s.id,
        "merchant": "Netflix",
        "old_cents": -1599,
        "new_cents": -1899,
        "occurred_on": "2026-07-15",
        "llm_flagged": True,
    }
    # second sync: open item suppresses both the re-ask and a duplicate item
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 0
    assert len(llm.prompts) == 1
    assert (await session.execute(select(ReviewItem))).scalar_one() is item


async def test_price_change_denied_resolution_suppresses(session: AsyncSession) -> None:
    s, _ = await _drifted_series(session)
    session.add(
        make_price_change_item(
            s.id,
            status=ReviewStatus.resolved,
            resolution={"is_price_change": False, "resolved_by": "manual"},
        )
    )
    await session.flush()
    llm = PriceLLM({"is_price_change": True, "confident": True})
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 0
    assert llm.prompts == []
    assert s.expected_cents == -1599
