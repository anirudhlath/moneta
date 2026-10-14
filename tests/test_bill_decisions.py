from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.models import (
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    recurring_cluster_item,
)
from moneta.pipelines.reconcile import reconcile_provider_overlap
from moneta.pipelines.review import apply_resolution, verify_series
from moneta.pipelines.transaction_overlap import _decisions, reviewed_merchant_occurrences
from tests.factories import make_account, make_series, make_txn


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    # Local copy of tests/test_api.py's fixture: moving that one to conftest would
    # drag its SNAP data and _client helper along, and these tests need neither.
    app = create_app(sessionmaker, adapters=[], llm=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _human(item: ReviewItem, is_recurring: bool) -> ReviewItem:
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": is_recurring, "resolved_by": "manual"}
    return item


async def test_specific_not_a_bill_ends_only_its_series(session: AsyncSession) -> None:
    sub = await make_series(session, merchant="Streamco", expected_cents=-999)
    orders = await make_series(session, merchant="Streamco", expected_cents=-2600)
    item = recurring_cluster_item("Streamco", "outflow", 2600)
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_recurring": False}, resolved_by="manual")
    assert orders.status == SeriesStatus.ended
    assert sub.status == SeriesStatus.active


async def test_human_answer_on_one_bill_does_not_block_llm_on_another(
    session: AsyncSession,
) -> None:
    session.add(_human(recurring_cluster_item("Streamco", "outflow", 2600), False))
    other = recurring_cluster_item("Streamco", "outflow", 999)
    merchant_level = recurring_cluster_item("Streamco", "outflow")
    session.add_all([other, merchant_level])
    await session.flush()
    await apply_resolution(session, other, {"is_recurring": True}, resolved_by="llm")
    assert other.status == ReviewStatus.resolved
    with pytest.raises(ValueError):
        await apply_resolution(session, merchant_level, {"is_recurring": True}, resolved_by="llm")


async def test_reviewed_occurrences_respect_bill_amounts(session: AsyncSession) -> None:
    acct = await make_account(session)
    sub = await make_txn(session, acct, amount_cents=-999, merchant="Streamco")
    order = await make_txn(session, acct, amount_cents=-2600, merchant="Streamco")
    session.add(_human(recurring_cluster_item("Streamco", "outflow", 999), True))
    await session.flush()
    roots = {sub.id: sub.id, order.id: order.id}
    assert await reviewed_merchant_occurrences(session, roots) == {sub.id}


class _BillLLM:
    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return {"classification": "bill", "confident": True}


async def test_verify_names_amounts_when_a_merchant_has_several_series(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for cents in (-999, -2600):
        s = await make_series(session, merchant="Streamco", expected_cents=cents)
        await make_txn(session, acct, amount_cents=cents, merchant="Streamco", series_id=s.id)
    await session.commit()
    stats = await verify_series(session, _BillLLM())
    assert stats.verified == 2
    items = (await session.execute(select(ReviewItem))).scalars().all()
    assert sorted(i.payload["amount_cents"] for i in items) == [999, 2600]


async def test_not_a_bill_endpoint_targets_one_of_several_series(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        sub = await make_series(session, merchant="Streamco", expected_cents=-999)
        orders = await make_series(session, merchant="Streamco", expected_cents=-2600)
        sub_id, orders_id = sub.id, orders.id
        await session.commit()
    r = await client.post(f"/recurring/{orders_id}/not-a-bill")
    assert r.status_code == 200
    async with sessionmaker() as session:
        statuses = {
            s.id: s.status for s in (await session.execute(select(RecurringSeries))).scalars()
        }
        item = (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalar_one()
    assert statuses == {sub_id: SeriesStatus.active, orders_id: SeriesStatus.ended}
    assert item.payload["amount_cents"] == 2600


async def test_verify_names_the_amount_when_another_bill_has_an_open_question(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    sub = await make_series(session, merchant="Streamco", expected_cents=-999)
    await make_txn(session, acct, amount_cents=-999, merchant="Streamco", series_id=sub.id)
    session.add(recurring_cluster_item("Streamco", "outflow", 2600))
    await session.commit()
    stats = await verify_series(session, _BillLLM())
    assert stats.verified == 1
    resolved = (
        await session.execute(select(ReviewItem).where(ReviewItem.status == ReviewStatus.resolved))
    ).scalar_one()
    assert resolved.payload["amount_cents"] == 999


async def test_re_review_survives_a_legacy_merchant_level_decision(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        await make_series(session, merchant="Streamco", expected_cents=-999)
        orders = await make_series(session, merchant="Streamco", expected_cents=-2600)
        session.add(_human(recurring_cluster_item("Streamco", "outflow"), True))
        orders_id = orders.id
        await session.commit()
    assert (await client.post(f"/recurring/{orders_id}/re-review")).status_code == 200
    async with sessionmaker() as session:
        await reconcile_provider_overlap(session, date(2026, 7, 15))
        specific = (
            await session.execute(
                select(ReviewItem).where(ReviewItem.payload["amount_cents"].as_integer() == 2600)
            )
        ).scalar_one()
    assert specific.status == ReviewStatus.open


async def test_specific_decision_beats_a_newer_merchant_level_one(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    txn = await make_txn(session, acct, amount_cents=-2600, merchant="Streamco")
    specific = _human(recurring_cluster_item("Streamco", "outflow", 2600), False)
    session.add(specific)
    await session.flush()
    newer = _human(recurring_cluster_item("Streamco", "outflow"), True)
    session.add(newer)
    await session.flush()
    assert specific.id < newer.id
    decisions = _decisions([txn], [], [specific, newer], [])
    assert decisions[txn.id]["recurring"] == (False, False)
