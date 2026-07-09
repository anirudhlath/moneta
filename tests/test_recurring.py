from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    Cadence,
    Direction,
    EventKind,
    RecurringSeries,
    ReviewItem,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.recurring import detect_recurring, monthly_cents
from tests.factories import make_account, make_txn


async def _series(session: AsyncSession) -> list[RecurringSeries]:
    return list((await session.execute(select(RecurringSeries))).scalars().all())


async def _seed_monthly(
    session: AsyncSession,
    months: tuple[int, ...],
    *,
    acct: Account | None = None,
    year: int = 2026,
    day: int = 15,
    merchant: str = "Netflix",
    cents: int = -1599,
) -> Account:
    """One txn per month on the given day — the standard monthly-series seeding."""
    acct = acct or await make_account(session)
    for month in months:
        await make_txn(
            session, acct, amount_cents=cents, merchant=merchant, posted_on=date(year, month, day)
        )
    return acct


async def test_monthly_subscription_detected(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6))
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.biweekly and s.direction == Direction.inflow


async def test_irregular_amounts_go_to_review(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1


async def test_non_recurring_ignored(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(
        session, acct, amount_cents=-5000, merchant="One Off", posted_on=date(2026, 6, 1)
    )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.merchant == "Synchrony Bank" and s.expected_cents == -13500


async def test_rerun_updates_not_duplicates(session: AsyncSession) -> None:
    acct = await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 7, 15)
    )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 16))
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
    stats = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
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
    stats = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    assert (await _series(session)) == []


async def test_resync_does_not_duplicate_missed_events(session: AsyncSession) -> None:
    await _seed_monthly(session, (1, 2, 3))
    today = date(2026, 5, 1)

    await detect_recurring(session, llm=None, today=today)
    await emit_series_events(session, today=today)
    await detect_recurring(session, llm=None, today=today)
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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1

    item = (await session.execute(select(ReviewItem))).scalar_one()
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": True}
    await session.commit()

    stats2 = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
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
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    item = (await session.execute(select(ReviewItem))).scalar_one()
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": False}
    await session.commit()

    # An LLM that would say "yes" proves force=False suppresses without even
    # consulting the LLM — distinguishing this from the pre-fix behavior.
    llm = _UnstableLLM({"is_recurring": True})
    stats2 = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.review == 1

    stats2 = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats2.new_series == 0 and stats2.review == 0
    assert (await _series(session)) == []
    reviews = (await session.execute(select(ReviewItem))).scalars().all()
    assert len(reviews) == 1


async def test_stale_history_creates_ended_series(session: AsyncSession) -> None:
    await _seed_monthly(session, (1, 2, 3), year=2025)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended
    assert s.next_expected_on == date(2025, 4, 14)  # still computed: 3/15 + 30


async def test_active_series_auto_ends_when_stale(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.active
    stats = await detect_recurring(session, llm=None, today=date(2026, 11, 1))  # 139 days stale
    assert stats.updated == 1
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended


async def test_manually_ended_series_stays_ended_without_new_activity(
    session: AsyncSession,
) -> None:
    await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    s.status = SeriesStatus.ended  # what PATCH /recurring/{id} does
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert s.status == SeriesStatus.ended


async def test_manually_ended_series_reactivates_on_new_occurrence(
    session: AsyncSession,
) -> None:
    acct = await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    s.status = SeriesStatus.ended
    await session.commit()
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 7, 15)
    )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 16))
    assert stats.updated == 1
    assert s.status == SeriesStatus.active
    assert s.next_expected_on == date(2026, 8, 14)


async def test_backfilled_old_txns_do_not_reactivate_ended_series(
    session: AsyncSession,
) -> None:
    acct = await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    s.status = SeriesStatus.ended
    await session.commit()
    # a deep re-pull (sync --full) backfills an OLDER txn; newest occurrence is unchanged
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 3, 15)
    )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert s.status == SeriesStatus.ended


async def test_historical_gap_does_not_poison_current_cadence(session: AsyncSession) -> None:
    # an old run at one price, a long break, then a current run at a new price
    acct = await _seed_monthly(session, (1, 2, 3), year=2025)
    await _seed_monthly(session, (4, 5, 6), acct=acct, cents=-1799)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.active
    assert s.expected_cents == -1799  # median of the current run, not the whole history
    assert s.next_expected_on == date(2026, 7, 15)


async def test_auto_ended_series_reactivates_when_cadence_reestablished(
    session: AsyncSession,
) -> None:
    acct = await _seed_monthly(session, (1, 2, 3), year=2025, merchant="Hulu")
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended  # auto-ended: stale history
    # resubscribed: three fresh occurrences re-establish the cadence
    await _seed_monthly(session, (5, 6, 7), acct=acct, day=1, merchant="Hulu", cents=-1899)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 2))
    assert stats.updated == 1
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.active
    assert s.next_expected_on == date(2026, 7, 31)


async def test_series_with_trailing_noise_still_auto_ends(session: AsyncSession) -> None:
    acct = await _seed_monthly(session, (4, 5, 6), merchant="Gym")
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    # a one-off purchase at the same merchant breaks the cadence run...
    await make_txn(session, acct, amount_cents=-2500, merchant="Gym", posted_on=date(2026, 7, 2))
    # ...and the merchant then goes dead; a much later sync must still auto-end the series
    stats = await detect_recurring(session, llm=None, today=date(2026, 12, 1))
    assert stats.updated == 1
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended


async def test_duplicate_same_day_charge_does_not_break_detection(
    session: AsyncSession,
) -> None:
    acct = await _seed_monthly(session, (4, 5, 6))
    # double-posted/retried charge on the newest date
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 6, 15)
    )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.monthly and s.status == SeriesStatus.active
    assert s.next_expected_on == date(2026, 7, 15)


async def test_stale_newer_backfill_does_not_reactivate_ended_series(
    session: AsyncSession,
) -> None:
    """Pins that staleness takes precedence over the untagged-newest reactivation check."""
    acct = await _seed_monthly(session, (1, 2, 3), year=2025)
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended  # auto-ended: stale history
    # a relinked account backfills an occurrence newer than anything stored — but ancient
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2025, 4, 15)
    )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended


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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "recurring_cluster"
    # re-run: no duplicate review item
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.review == 0


async def test_cadence_miss_force_accept_uses_closest_cadence(session: AsyncSession) -> None:
    from moneta.models import ReviewItem, ReviewStatus

    acct = await make_account(session)
    for d in (date(2026, 4, 1), date(2026, 4, 11), date(2026, 5, 26)):
        await make_txn(session, acct, amount_cents=-30000, merchant="Odd Timing Co", posted_on=d)
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    item = (await session.execute(select(ReviewItem))).scalar_one()
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": True}
    await session.flush()
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
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
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 0
    assert (await session.execute(select(ReviewItem))).scalars().all() == []
