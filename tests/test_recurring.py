from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    AccountType,
    Cadence,
    Direction,
    EventKind,
    RecurringSeries,
    ReviewItem,
    ReviewStatus,
    SeriesEvent,
    Transaction,
    TransferLink,
)
from moneta.pipelines.events import emit_series_events
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


class _UnstableLLM:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return self.answer


async def test_llm_gates_but_never_sets_expected_amount(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    llm = _UnstableLLM({"is_recurring": True, "expected_amount_cents": 999999})
    stats = await detect_recurring(session, llm=llm)
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.expected_cents == -4500  # deterministic signed median, never the LLM's number


async def test_llm_rejects_group_creates_review_no_series(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    llm = _UnstableLLM({"is_recurring": False})
    stats = await detect_recurring(session, llm=llm)
    assert stats.new_series == 0 and stats.review == 1
    assert (await _series(session)) == []


async def test_resync_does_not_duplicate_missed_events(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, day in ((1, 15), (2, 15), (3, 15)):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, month, day)
        )
    today = date(2026, 5, 1)

    await detect_recurring(session, llm=None)
    await emit_series_events(session, today=today)
    await detect_recurring(session, llm=None)
    await emit_series_events(session, today=today)

    missed = (
        (await session.execute(select(SeriesEvent).where(SeriesEvent.kind == EventKind.missed)))
        .scalars()
        .all()
    )
    assert len(missed) == 1
    s = (await _series(session))[0]
    assert s.next_expected_on == date(2026, 5, 14)  # advance retained, not rewound


async def test_recurring_cluster_resolved_true_creates_series_from_median(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 0 and stats.review == 1

    item = (await session.execute(select(ReviewItem))).scalar_one()
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": True}
    await session.commit()

    stats2 = await detect_recurring(session, llm=None)
    assert stats2.new_series == 1 and stats2.review == 0
    s = (await _series(session))[0]
    assert s.merchant == "Util Co" and s.expected_cents == -4500  # deterministic median
    reviews = (await session.execute(select(ReviewItem))).scalars().all()
    assert len(reviews) == 1  # no duplicate ReviewItem opened


async def test_recurring_cluster_resolved_false_suppresses_series_forever(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    await detect_recurring(session, llm=None)

    item = (await session.execute(select(ReviewItem))).scalar_one()
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": False}
    await session.commit()

    # An LLM that would say "yes" proves force=False suppresses without even
    # consulting the LLM — distinguishing this from the pre-fix behavior.
    llm = _UnstableLLM({"is_recurring": True})
    stats2 = await detect_recurring(session, llm=llm)
    assert stats2.new_series == 0 and stats2.review == 0
    assert (await _series(session)) == []
    reviews = (await session.execute(select(ReviewItem))).scalars().all()
    assert len(reviews) == 1  # no new ReviewItem opened


async def test_recurring_cluster_still_open_no_series_no_duplicate_review(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    stats = await detect_recurring(session, llm=None)
    assert stats.review == 1

    stats2 = await detect_recurring(session, llm=None)
    assert stats2.new_series == 0 and stats2.review == 0
    assert (await _series(session)) == []
    reviews = (await session.execute(select(ReviewItem))).scalars().all()
    assert len(reviews) == 1


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


async def test_tiny_adjustment_charge_ignored_for_cadence(session: AsyncSession) -> None:
    """A sub-scale adjustment (e.g. $1.89 insurance true-up) must not break cadence."""
    acct = await make_account(session)
    await make_txn(
        session, acct, amount_cents=-26665, merchant="Tesla Insurance", posted_on=date(2026, 4, 27)
    )
    await make_txn(
        session, acct, amount_cents=-189, merchant="Tesla Insurance", posted_on=date(2026, 5, 12)
    )  # the poison pill
    await make_txn(
        session, acct, amount_cents=-22522, merchant="Tesla Insurance", posted_on=date(2026, 5, 24)
    )
    await make_txn(
        session, acct, amount_cents=-25059, merchant="Tesla Insurance", posted_on=date(2026, 6, 27)
    )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.monthly
    assert s.expected_cents == -25059  # median of the three real charges
    tiny = (
        await session.execute(select(Transaction).where(Transaction.amount_cents == -189))
    ).scalar_one()
    assert tiny.series_id is None  # adjustment stays out of the series


async def test_cadence_miss_goes_to_review(session: AsyncSession) -> None:
    from moneta.models import ReviewItem

    acct = await make_account(session)
    for d in (date(2026, 4, 1), date(2026, 4, 11), date(2026, 5, 26)):
        await make_txn(session, acct, amount_cents=-30000, merchant="Odd Timing Co", posted_on=d)
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 0 and stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "recurring_cluster"
    # re-run: no duplicate review item
    stats = await detect_recurring(session, llm=None)
    assert stats.review == 0


async def test_cadence_miss_force_accept_uses_closest_cadence(session: AsyncSession) -> None:
    from moneta.models import ReviewItem, ReviewStatus

    acct = await make_account(session)
    for d in (date(2026, 4, 1), date(2026, 4, 11), date(2026, 5, 26)):
        await make_txn(session, acct, amount_cents=-30000, merchant="Odd Timing Co", posted_on=d)
    await detect_recurring(session, llm=None)
    item = (await session.execute(select(ReviewItem))).scalar_one()
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": True}
    await session.flush()
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.monthly  # median gap 27.5 days → closest cadence
    assert s.expected_cents == -30000


async def test_cadence_miss_unstable_amounts_stays_silent(session: AsyncSession) -> None:
    from moneta.models import ReviewItem

    acct = await make_account(session)
    for d, cents in (
        (date(2026, 4, 1), -1200),
        (date(2026, 4, 11), -6500),
        (date(2026, 5, 26), -3100),
    ):
        await make_txn(session, acct, amount_cents=cents, merchant="Random Restaurant", posted_on=d)
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 0 and stats.review == 0
    assert (await session.execute(select(ReviewItem))).scalars().all() == []


async def test_cadence_miss_habitual_frequency_stays_silent(session: AsyncSession) -> None:
    """Stable amounts but bursty sub-weekly timing = coffee habit, not a bill."""
    from datetime import timedelta

    from moneta.models import ReviewItem

    acct = await make_account(session)
    start = date(2026, 6, 1)
    for offset in (0, 2, 5, 6, 10, 13):  # median gap ~3 days, irregular
        await make_txn(
            session,
            acct,
            amount_cents=-650,
            merchant="Corner Coffee",
            posted_on=start + timedelta(days=offset),
        )
    stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 0 and stats.review == 0
    assert (await session.execute(select(ReviewItem))).scalars().all() == []
