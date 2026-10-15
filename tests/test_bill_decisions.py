from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.models import (
    AccountType,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    Transaction,
    recurring_cluster_item,
)
from moneta.pipelines.reconcile import reconcile_provider_overlap
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.review import apply_resolution, verify_series
from moneta.pipelines.transaction_overlap import _decisions, reviewed_merchant_occurrences
from moneta.views.cashflow import accrual_by_month
from moneta.views.power import power_report
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


async def test_verify_knows_a_variable_bill_by_its_median(session: AsyncSession) -> None:
    # a variable bill's expected amount is its typical charge, which can sit far from the
    # median detection keyed the decision by: the decided bill must not be asked again
    acct = await make_account(session)
    s = await make_series(session, merchant="Streamco", expected_cents=-1000)
    for month, cents in ((3, -1000), (4, -4000), (5, -2500), (6, -1000), (7, -3000)):
        await make_txn(
            session,
            acct,
            amount_cents=cents,
            merchant="Streamco",
            posted_on=date(2026, month, 10),
            series_id=s.id,
        )
    session.add(_human(recurring_cluster_item("Streamco", "outflow", 2500), True))
    await session.commit()
    stats = await verify_series(session, _BillLLM())
    assert stats.verified == 0 and stats.flagged == 0


async def test_verify_knows_a_lone_bill_by_any_amount_it_charges(session: AsyncSession) -> None:
    # the decision was made when charges centred on $10; they now centre on $17
    acct = await make_account(session)
    s = await make_series(session, merchant="Streamco", expected_cents=-1700)
    for month, cents in ((1, -1000), (2, -1700), (3, -1700), (4, -2500), (5, -400), (6, -1700)):
        await make_txn(
            session,
            acct,
            amount_cents=cents,
            merchant="Streamco",
            posted_on=date(2026, month, 10),
            series_id=s.id,
        )
    session.add(_human(recurring_cluster_item("Streamco", "outflow", 1000), True))
    await session.commit()
    stats = await verify_series(session, _BillLLM())
    assert stats.verified == 0 and stats.flagged == 0


# One merchant, two bills: a steady 4000, and a variable one. The variable bill's
# series is expected at its most-repeated charge (11800), which sits more than 20% from
# the median of its charges (8200), the amount detection keys its decisions by.
_VARIABLE_BILL = [
    11800,
    8000,
    8100,
    11800,
    8200,
    8300,
    11800,
    9900,
    8400,
    8000,
    11800,
    7900,
    8000,
    8100,
]


class _SwitchLLM:
    """Answers detection's bill/habit/not_recurring prompt with whatever it was set to."""

    def __init__(self, classification: str = "bill") -> None:
        self.classification = classification

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return {"classification": self.classification}


def _status(series: RecurringSeries) -> str:
    """The status as a plain str, so a later `== ended` isn't narrowed away by mypy."""
    return series.status


def _month_day(index: int, day: int) -> date:
    """The given day, `index` months after 2025-06."""
    year, month0 = divmod(2025 * 12 + 5 + index, 12)
    return date(year, month0 + 1, day)


async def _two_bills_one_merchant(
    session: AsyncSession, sign: int
) -> tuple[Callable[[int, int, int], Awaitable[None]], RecurringSeries, RecurringSeries]:
    """Detect both bills with an LLM that calls the variable one a bill. Returns the
    charge helper and the (steady, variable) series. sign is -1 for outflows."""
    merchant = "Acme Gym" if sign < 0 else "Jane Doe"
    account = await make_account(session, type=AccountType.checking)

    async def charge(index: int, day: int, cents: int) -> None:
        await make_txn(
            session,
            account,
            amount_cents=sign * cents,
            posted_on=_month_day(index, day),
            description=merchant,
            merchant=merchant,
        )

    for index, cents in enumerate(_VARIABLE_BILL):
        await charge(index, 3, cents)
        await charge(index, 15, 4000)
    await detect_recurring(session, _SwitchLLM("bill"), date(2026, 7, 20))
    by_expected = {abs(s.expected_cents): s for s in await session.scalars(select(RecurringSeries))}
    assert sorted(by_expected) == [4000, 11800]
    return charge, by_expected[4000], by_expected[11800]


@pytest.mark.parametrize("sign", [-1, 1], ids=["outflow", "inflow"])
async def test_not_recurring_answer_ends_a_bill_it_could_not_name(
    session: AsyncSession, sign: int
) -> None:
    """The question names the bill by its median charge (8200); apply_resolution matches
    series by their expected amount (11800), so it skips this one. Detection matches by
    median, honors the answer, and must end the series it leaves with no charges, or
    power would count it as a fixed cost forever and its charges as spend as well."""
    charge, _steady, variable = await _two_bills_one_merchant(session, sign)
    await charge(14, 3, 8100)  # 2026-08
    await charge(14, 15, 4000)
    await detect_recurring(session, _SwitchLLM("not_recurring"), date(2026, 8, 20))
    item = (
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == ReviewKind.recurring_cluster,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    ).one()
    assert item.payload["amount_cents"] == 8200
    await apply_resolution(session, item, {"is_recurring": False}, resolved_by="manual")
    await session.commit()
    assert _status(variable) == SeriesStatus.active  # the answer's amount skipped it

    # the owner's answer covers the bill for detection even when the LLM still says bill
    await detect_recurring(session, _SwitchLLM("bill"), date(2026, 8, 20))
    assert variable.status == SeriesStatus.ended
    assert (
        await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.series_id == variable.id)
        )
        == 0
    )

    await charge(15, 3, 8100)  # 2026-09: the next sync keeps it ended
    await charge(15, 15, 4000)
    await detect_recurring(session, _SwitchLLM("bill"), date(2026, 9, 20))
    assert variable.status == SeriesStatus.ended

    today = date(2026, 8, 20)
    report = await power_report(session, today)
    row = (await accrual_by_month(session, months=1, today=today))[0]
    if sign < 0:
        assert report.total_fixed_cents == 4000  # the steady bill only
        assert report.spent_so_far_cents == 8100  # the variable bill's charge, once
    else:
        assert report.monthly_income_cents == 4000
        assert row.other_inflows_cents == 8100  # the variable bill's charge, once


@pytest.mark.parametrize("sign", [-1, 1], ids=["outflow", "inflow"])
async def test_not_a_bill_on_a_variable_bill_holds_across_syncs(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession], sign: int
) -> None:
    """`--not-a-bill` names the series by its expected amount (11800); detection matches
    decisions by the median (8200), so it does not see this answer as covering the bill.
    The series keeps its tags, so detection finds the ended series again instead of
    building a fresh active one for the same bill."""
    async with sessionmaker() as session:
        _charge, steady, variable = await _two_bills_one_merchant(session, sign)
        steady_id, variable_id = steady.id, variable.id
    assert (await client.post(f"/recurring/{variable_id}/not-a-bill")).status_code == 200
    async with sessionmaker() as session:
        for today in (date(2026, 7, 20), date(2026, 8, 20)):
            await detect_recurring(session, _SwitchLLM("bill"), today)
            statuses = {s.id: s.status for s in await session.scalars(select(RecurringSeries))}
            assert statuses == {steady_id: SeriesStatus.active, variable_id: SeriesStatus.ended}
        report = await power_report(session, date(2026, 8, 20))
    assert (report.total_fixed_cents if sign < 0 else report.monthly_income_cents) == 4000
