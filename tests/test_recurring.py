from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import advance_expected_on, monthly_cents
from moneta.models import (
    Account,
    AccountType,
    Cadence,
    Direction,
    EventKind,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
    Transaction,
    TransferLink,
    recurring_cluster_item,
)
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.normalize import normalize_merchants, renormalize_merchants
from moneta.pipelines.recurring import detect_recurring
from moneta.views.power import power_report
from tests.factories import make_account, make_series, make_txn


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


async def test_internal_transfers_and_loan_payments_excluded(session: AsyncSession) -> None:
    """Internal transfers were always excluded from merchant grouping. Wave-2 Task 8
    changed loan-linked payment outflows from "kept" to also excluded — their
    per-account cadence/amount is now derived in queries.loan_payment_stats instead
    of forming their own merchant series (design 2026-07-16 §3)."""
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
        # loan payment checking->loan: excluded from grouping too (Task 8)
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
    assert stats.new_series == 0
    assert (await _series(session)) == []


async def test_loan_linked_payments_form_no_series(session: AsyncSession) -> None:
    """Three monthly outflows to the same merchant, each transfer-linked to a loan
    account inflow, must never form a merchant series — loan_payment_stats owns
    their per-account derivation instead."""
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan)
    for month in (4, 5, 6):
        out = await make_txn(
            session,
            checking,
            amount_cents=-13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(session, loan, amount_cents=13500, posted_on=date(2026, month, 5))
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
    await session.flush()
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0
    assert (await _series(session)) == []


async def test_existing_merged_payment_series_ends(session: AsyncSession) -> None:
    """A series that existed before Task 8 shipped, whose occurrences are entirely
    loan-linked payment outflows, gets untagged and ended on the first run after."""
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan)
    series = await make_series(
        session,
        merchant="Synchrony Bank",
        expected_cents=-13500,
        next_expected_on=date(2026, 7, 5),
    )
    outflow_ids: list[int] = []
    for month in (4, 5, 6):
        out = await make_txn(
            session,
            checking,
            amount_cents=-13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
            series_id=series.id,
        )
        inn = await make_txn(session, loan, amount_cents=13500, posted_on=date(2026, month, 5))
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
        outflow_ids.append(out.id)
    await session.flush()
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert stats.new_series == 0
    assert series.status == SeriesStatus.ended
    untagged = (
        (await session.execute(select(Transaction).where(Transaction.id.in_(outflow_ids))))
        .scalars()
        .all()
    )
    assert all(t.series_id is None for t in untagged)


async def test_partial_untag_keeps_series(session: AsyncSession) -> None:
    """A series with both loan-linked and genuine occurrences loses only the
    loan-linked ones; the genuine occurrences keep it active."""
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan)
    series = await make_series(
        session,
        merchant="Synchrony Bank",
        expected_cents=-13500,
        next_expected_on=date(2026, 3, 5),
    )
    # two old occurrences, already tagged, that turn out to be loan-linked payments
    old_ids: list[int] = []
    for month in (1, 2):
        out = await make_txn(
            session,
            checking,
            amount_cents=-13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
            series_id=series.id,
        )
        inn = await make_txn(session, loan, amount_cents=13500, posted_on=date(2026, month, 5))
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
        old_ids.append(out.id)
    # three genuine, unlinked occurrences under the same merchant
    genuine_ids: list[int] = []
    for month in (4, 5, 6):
        t = await make_txn(
            session,
            checking,
            amount_cents=-13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
        )
        genuine_ids.append(t.id)
    await session.flush()
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert stats.new_series == 0
    assert series.status == SeriesStatus.active
    old = (
        (await session.execute(select(Transaction).where(Transaction.id.in_(old_ids))))
        .scalars()
        .all()
    )
    assert all(t.series_id is None for t in old)
    genuine = (
        (await session.execute(select(Transaction).where(Transaction.id.in_(genuine_ids))))
        .scalars()
        .all()
    )
    assert all(t.series_id == series.id for t in genuine)


async def test_rerun_updates_not_duplicates(session: AsyncSession) -> None:
    acct = await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 7, 15)
    )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 16))
    assert stats.new_series == 0 and stats.updated == 1
    s = (await _series(session))[0]
    assert s.next_expected_on == date(2026, 8, 15)  # 7/15 + 1 month


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
    llm = _UnstableLLM({"classification": "bill", "expected_amount_cents": 999999})
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
    llm = _UnstableLLM({"classification": "not_recurring"})
    stats = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    assert (await _series(session)) == []


async def _seed_weekly_unstable(
    session: AsyncSession, merchant: str = "Neighborhood Bistro"
) -> None:
    """4 weekly-cadence charges with a >2x amount spread — cadence matches, amount doesn't,
    so detection must ask the three-way bill/habit/not_recurring question."""
    acct = await make_account(session)
    for d, cents in (
        (date(2026, 6, 1), -2176),
        (date(2026, 6, 8), -12035),
        (date(2026, 6, 15), -3886),
        (date(2026, 6, 22), -4100),
    ):
        await make_txn(session, acct, amount_cents=cents, merchant=merchant, posted_on=d)


async def test_unstable_llm_habit_becomes_discretionary(session: AsyncSession) -> None:
    await _seed_weekly_unstable(session)
    llm = _UnstableLLM({"classification": "habit"})
    stats = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.weekly
    assert s.discretionary is True


async def test_unstable_llm_bill_stays_fixed(session: AsyncSession) -> None:
    await _seed_weekly_unstable(session)
    llm = _UnstableLLM({"classification": "bill"})
    stats = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.weekly
    assert s.discretionary is False


async def test_variable_bill_expects_its_repeating_amount(session: AsyncSession) -> None:
    # a monthly fee with occasional larger charges at the same shop: the fee repeats,
    # so it is the bill's amount; the median would land between fee and extras
    acct = await make_account(session)
    for month, cents in ((3, -3000), (4, -15000), (5, -30000), (6, -3000)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Acme Lab", posted_on=date(2026, month, 20)
        )
    llm = _UnstableLLM({"classification": "bill"})
    await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.expected_cents == -3000


async def test_existing_variable_bill_follows_its_typical_amount(session: AsyncSession) -> None:
    # an existing series whose amounts vary keeps up with them every sync: its last
    # one-off price must not stand in for the bill
    acct = await make_account(session)
    series = await make_series(session, merchant="Acme Device", expected_cents=-15000)
    for month, cents in ((2, -11000), (3, -8000), (4, -11000), (5, -16000), (6, -15000)):
        await make_txn(
            session,
            acct,
            amount_cents=cents,
            merchant="Acme Device",
            posted_on=date(2026, month, 25),
            series_id=series.id,
        )
    llm = _UnstableLLM({"classification": "bill"})
    stats = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    assert stats.new_series == 0
    s = (await _series(session))[0]
    assert s.id == series.id and s.expected_cents == -11000


async def test_variable_bill_judges_its_newest_charges_only(session: AsyncSession) -> None:
    # a long biweekly run whose old pay repeats more often than today's: only the newest
    # twelve deposits say what the paycheck is now
    acct = await make_account(session)
    series = await make_series(
        session, merchant="Acme Payroll", direction=Direction.inflow, expected_cents=150000
    )
    recent = [200000] * 4 + [700000] + [190000 + 1000 * i for i in range(7)]
    for i, cents in enumerate([300000] * 14 + recent):
        await make_txn(
            session,
            acct,
            amount_cents=cents,
            merchant="Acme Payroll",
            posted_on=date(2025, 7, 4) + timedelta(days=14 * i),
            series_id=series.id,
        )
    llm = _UnstableLLM({"classification": "bill"})
    await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.id == series.id and s.expected_cents == 200000


async def test_existing_stable_bill_keeps_its_confirmed_price(session: AsyncSession) -> None:
    # a steady bill's price moves only through events' two-occurrence agreement
    acct = await make_account(session)
    series = await make_series(session, merchant="Acme Stream", expected_cents=-1500)
    for month, cents in ((4, -1500), (5, -1500), (6, -1650)):
        await make_txn(
            session,
            acct,
            amount_cents=cents,
            merchant="Acme Stream",
            posted_on=date(2026, month, 15),
            series_id=series.id,
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.expected_cents == -1500


async def test_unstable_llm_not_recurring_opens_review(session: AsyncSession) -> None:
    await _seed_weekly_unstable(session)
    llm = _UnstableLLM({"classification": "not_recurring"})
    stats = await detect_recurring(session, llm=llm, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    assert (await _series(session)) == []
    item = (await session.execute(select(ReviewItem))).scalar_one()
    # the LLM already looked and declined — human-only from here (task 16, 2026-07-16)
    assert item.payload["llm_flagged"] is True


async def test_unstable_no_llm_opens_review(session: AsyncSession) -> None:
    """Unchanged behavior: with no LLM configured, an unstable-amount group still
    degrades to a human review item rather than guessing bill vs. habit."""
    await _seed_weekly_unstable(session)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    assert (await _series(session)) == []
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert "llm_flagged" not in item.payload  # no LLM ever looked — still autoreviewable


async def test_stable_subscription_unchanged(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6))
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.discretionary is False


async def test_force_map_habit_applies_on_update(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6), merchant="Habit Co")
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.discretionary is False

    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            status=ReviewStatus.resolved,
            question="Is 'Habit Co' a recurring bill?",
            payload={"merchant": "Habit Co", "direction": Direction.outflow},
            resolution={"is_recurring": True, "discretionary": True},
        )
    )
    await session.commit()

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.updated == 1
    s = (await _series(session))[0]
    assert s.discretionary is True


async def test_resync_does_not_duplicate_missed_events(session: AsyncSession) -> None:
    await _seed_monthly(session, (1, 2, 3))
    today = date(2026, 5, 1)

    await detect_recurring(session, llm=None, today=today)
    await emit_series_events(session, llm=None, today=today)
    await detect_recurring(session, llm=None, today=today)
    await emit_series_events(session, llm=None, today=today)

    missed = (
        (await session.execute(select(SeriesEvent).where(SeriesEvent.kind == EventKind.missed)))
        .scalars()
        .all()
    )
    assert len(missed) == 1
    s = (await _series(session))[0]
    assert s.next_expected_on == date(2026, 5, 15)  # advance retained, not rewound


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
    llm = _UnstableLLM({"classification": "bill"})
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
    assert s.next_expected_on == date(2025, 4, 15)  # still computed: 3/15 + 1 month


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
    assert s.next_expected_on == date(2026, 8, 15)


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
    assert s.next_expected_on == date(2026, 8, 1)


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
    # newest gap 45 days is nearest monthly (30) but outside its tolerance (6) → falls
    # back to monthly anyway, the safe default
    assert s.cadence == Cadence.monthly
    assert s.expected_cents == -30000


async def test_forced_cadence_prefers_newest_gap(session: AsyncSession) -> None:
    """Lifetime Fitness: a prorated first charge left gaps [12, 30]. The all-history
    median gap (21) sits nearest biweekly; the newest gap (30) is monthly and is the
    least-stale evidence — picking biweekly would show $575/mo for a $265.72/mo gym."""
    acct = await make_account(session)
    start = date(2026, 4, 1)
    for offset in (0, 12, 42):
        await make_txn(
            session,
            acct,
            amount_cents=-26572,
            merchant="Lifetime Fitness",
            posted_on=start + timedelta(days=offset),
        )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": True}
    await session.commit()

    stats2 = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats2.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.monthly
    assert s.expected_cents == -26572


async def test_forced_single_date_monthly_no_crash(session: AsyncSession) -> None:
    """A group whose significant txns dedup to a single posted_on date must fall back
    to monthly without statistics.median raising on an empty gap list."""
    acct = await make_account(session)
    same_day = date(2026, 6, 15)
    for _ in range(3):
        await make_txn(
            session, acct, amount_cents=-5000, merchant="Same Day Co", posted_on=same_day
        )
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            status=ReviewStatus.resolved,
            question="Is 'Same Day Co' a recurring bill?",
            payload={"merchant": "Same Day Co", "direction": Direction.outflow},
            resolution={"is_recurring": True},
        )
    )
    await session.commit()

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.cadence == Cadence.monthly


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


def test_advance_monthly_keeps_day_of_month() -> None:
    assert advance_expected_on(date(2026, 1, 1), Cadence.monthly) == date(2026, 2, 1)
    assert advance_expected_on(date(2026, 1, 31), Cadence.monthly) == date(2026, 2, 28)
    assert advance_expected_on(date(2026, 4, 30), Cadence.monthly) == date(2026, 5, 30)


def test_advance_annual_and_fixed_cadences() -> None:
    assert advance_expected_on(date(2026, 2, 28), Cadence.annual) == date(2027, 2, 28)
    assert advance_expected_on(date(2026, 7, 1), Cadence.weekly) == date(2026, 7, 8)
    assert advance_expected_on(date(2026, 7, 1), Cadence.biweekly) == date(2026, 7, 15)


async def test_foreign_currency_txns_never_form_series(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.checking)  # USD majority (tie → USD)
    eur = await make_account(session, type=AccountType.checking, currency="EUR")
    for month in (4, 5, 6):
        await make_txn(
            session, eur, amount_cents=-999, merchant="Spotify", posted_on=date(2026, month, 15)
        )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 0


async def test_resumed_series_backfills_skipped_missed_windows(session: AsyncSession) -> None:
    acct = await _seed_monthly(session, (1, 2, 3))
    await detect_recurring(session, llm=None, today=date(2026, 3, 20))
    s = (await _series(session))[0]
    assert s.next_expected_on == date(2026, 4, 15)

    # April's charge never arrives; the series resumes May–July before the next sync,
    # so detection leaps next_expected_on from 4/15 to 8/15 over the empty April window
    # (a resumed run needs 3 occurrences before cadence re-establishes — see _match_cadence)
    await _seed_monthly(session, (5, 6, 7), acct=acct)
    await detect_recurring(session, llm=None, today=date(2026, 7, 20))
    missed = (
        (await session.execute(select(SeriesEvent).where(SeriesEvent.kind == EventKind.missed)))
        .scalars()
        .all()
    )
    assert [e.occurred_on for e in missed] == [date(2026, 4, 15)]
    assert s.next_expected_on == date(2026, 8, 15)

    # idempotent: re-running detection emits nothing new
    await detect_recurring(session, llm=None, today=date(2026, 7, 20))
    missed = (
        (await session.execute(select(SeriesEvent).where(SeriesEvent.kind == EventKind.missed)))
        .scalars()
        .all()
    )
    assert len(missed) == 1


async def test_backfill_never_emits_future_windows(session: AsyncSession) -> None:
    await _seed_monthly(session, (1, 2, 3), day=1)
    await detect_recurring(session, llm=None, today=date(2026, 3, 2))
    s = (await _series(session))[0]
    assert s.next_expected_on == date(2026, 4, 1)

    # the next charge posts 8 days late, re-anchoring the grid to the 9th
    acct = (await session.execute(select(Account))).scalars().first()
    assert acct is not None
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 4, 9)
    )
    await detect_recurring(session, llm=None, today=date(2026, 4, 10))
    missed = (
        (await session.execute(select(SeriesEvent).where(SeriesEvent.kind == EventKind.missed)))
        .scalars()
        .all()
    )
    # the 4/1 window genuinely had no charge within grace; 5/1 hasn't happened yet
    assert [e.occurred_on for e in missed] == [date(2026, 4, 1)]


async def test_detection_untags_txns_whose_merchant_moved(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    newest = (
        await session.execute(select(Transaction).order_by(Transaction.posted_on.desc()).limit(1))
    ).scalar_one()
    assert newest.series_id == s.id
    newest.merchant = "Spotify Usa"  # a description correction re-derived the merchant

    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    await session.refresh(newest)
    assert newest.series_id != s.id  # no longer feeds the Netflix series' stats


async def test_same_merchant_tagged_txn_never_revives_ended_series(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    s.status = SeriesStatus.ended  # user cancels it
    await session.commit()

    # a description-only upstream correction leaves the txn tagged (see ingest.py);
    # detection must not read it as a genuinely-new occurrence and revive the series
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    await session.refresh(s)
    assert s.status == SeriesStatus.ended


async def _seed_sub_and_orders(session: AsyncSession) -> tuple[Account, list[Transaction]]:
    acct = await _seed_monthly(session, (3, 4, 5, 6), merchant="Streamco", cents=-999)
    orders = [
        await make_txn(session, acct, amount_cents=c, merchant="Streamco", posted_on=d)
        for d, c in (
            (date(2026, 3, 3), -2350),
            (date(2026, 4, 9), -1720),
            (date(2026, 5, 20), -3100),
            (date(2026, 6, 2), -2640),
        )
    ]
    return acct, orders


async def test_subscription_with_orders_forms_one_series(session: AsyncSession) -> None:
    _, orders = await _seed_sub_and_orders(session)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1 and stats.review == 0
    s = (await _series(session))[0]
    assert s.expected_cents == -999 and s.cadence == Cadence.monthly
    assert all(o.series_id is None for o in orders)


async def test_subscription_keeps_its_series_beside_many_small_purchases(
    session: AsyncSession,
) -> None:
    acct = await _seed_monthly(session, (1, 2, 3, 4, 5, 6), merchant="Streamco", cents=-1999)
    small = [
        await make_txn(session, acct, amount_cents=c, merchant="Streamco", posted_on=d)
        for d, c in (
            (date(2026, 1, 17), -299),
            (date(2026, 1, 20), -499),
            (date(2026, 2, 14), -349),
            (date(2026, 2, 17), -450),
            (date(2026, 3, 20), -299),
            (date(2026, 3, 22), -399),
            (date(2026, 4, 25), -499),
            (date(2026, 5, 28), -320),
            (date(2026, 6, 1), -380),
        )
    ]
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    [s] = await _series(session)
    assert s.status == SeriesStatus.active
    assert s.expected_cents == -1999 and s.cadence == Cadence.monthly
    subs = (
        (await session.execute(select(Transaction).where(Transaction.amount_cents == -1999)))
        .scalars()
        .all()
    )
    assert len(subs) == 6 and all(t.series_id == s.id for t in subs)
    assert all(t.series_id is None for t in small)


async def test_top_ups_are_untagged_from_an_existing_series(session: AsyncSession) -> None:
    _, orders = await _seed_sub_and_orders(session)
    old = await make_series(session, merchant="Streamco", expected_cents=-999)
    for t in (await session.execute(select(Transaction))).scalars():
        t.series_id = old.id  # pre-split state: one series swallowed everything
    await session.commit()
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0
    s = (await _series(session))[0]
    assert s.id == old.id
    assert all(o.series_id is None for o in orders)


async def test_variable_bill_still_goes_to_review(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((3, -8000), (4, -17000), (5, -9500), (6, -12000)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert "amount_cents" not in item.payload  # one bill: merchant-level question


async def test_concurrent_subscriptions_are_stable_across_runs(session: AsyncSession) -> None:
    acct = await _seed_monthly(session, (1, 2, 3, 4, 5, 6), merchant="Chatco", day=11, cents=-2000)
    await _seed_monthly(session, (4, 5, 6), acct=acct, merchant="Chatco", day=15, cents=-2000)
    first = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert first.new_series == 2
    assert {s.next_expected_on for s in await _series(session)} == {
        date(2026, 7, 11),
        date(2026, 7, 15),
    }
    second = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert second.new_series == 0 and second.merged == 0
    assert len(await _series(session)) == 2


async def test_payer_streams_at_different_amounts_form_two_series(session: AsyncSession) -> None:
    acct = await make_account(session)
    start = date(2026, 5, 1)
    for i in range(5):
        await make_txn(
            session,
            acct,
            amount_cents=150000,
            merchant="Acme Corp",
            posted_on=start + timedelta(days=14 * i),
        )
        await make_txn(
            session,
            acct,
            amount_cents=200000,
            merchant="Acme Corp",
            posted_on=start + timedelta(days=7 + 14 * i),
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 9))
    series = await _series(session)
    assert sorted(s.expected_cents for s in series) == [150000, 200000]
    assert {s.cadence for s in series} == {Cadence.biweekly}


async def test_pre_merge_series_fold_into_one(session: AsyncSession) -> None:
    acct = await make_account(session)
    older = await make_series(
        session, merchant="Acme Fitness", expected_cents=-4999, status=SeriesStatus.ended
    )
    newer = await make_series(session, merchant="Acme Fitness", expected_cents=-4999)
    for month in (1, 2, 3, 4, 5, 6):
        await make_txn(
            session,
            acct,
            amount_cents=-4999,
            merchant="Acme Fitness",
            posted_on=date(2026, month, 3),
            series_id=older.id if month <= 3 else newer.id,
        )
    session.add(
        SeriesEvent(
            series_id=older.id, kind=EventKind.missed, occurred_on=date(2026, 4, 3), details={}
        )
    )
    session.add(
        ReviewItem(kind=ReviewKind.price_change, question="?", payload={"series_id": older.id})
    )
    await session.commit()

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    assert stats.merged == 1
    (survivor,) = await _series(session)
    assert survivor.id == newer.id  # it tags the newest charge
    assert survivor.status == SeriesStatus.active
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert {t.series_id for t in txns} == {newer.id}
    events = (await session.execute(select(SeriesEvent))).scalars().all()
    assert {e.series_id for e in events} <= {newer.id}
    price = (
        await session.execute(select(ReviewItem).where(ReviewItem.kind == ReviewKind.price_change))
    ).scalar_one()
    assert price.payload["series_id"] == newer.id


async def test_per_bill_decision_only_forces_its_bill(session: AsyncSession) -> None:
    await _seed_sub_and_orders(session)
    orders_item = recurring_cluster_item("Streamco", "outflow", 2600)
    orders_item.status = ReviewStatus.resolved
    orders_item.resolution = {"is_recurring": False, "resolved_by": "manual"}
    session.add(orders_item)
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert [s.expected_cents for s in await _series(session)] == [-999]


async def test_merchant_level_decision_covers_every_bill(session: AsyncSession) -> None:
    await _seed_sub_and_orders(session)
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="?",
            payload={"merchant": "Streamco", "direction": "outflow"},
            status=ReviewStatus.resolved,
            resolution={"is_recurring": False, "resolved_by": "manual"},
        )
    )
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert await _series(session) == []


async def test_specific_decision_beats_newer_merchant_level(session: AsyncSession) -> None:
    await _seed_sub_and_orders(session)
    specific = recurring_cluster_item("Streamco", "outflow", 999)
    specific.status = ReviewStatus.resolved
    specific.resolution = {"is_recurring": True, "discretionary": True, "resolved_by": "manual"}
    session.add(specific)
    await session.flush()
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="?",
            payload={"merchant": "Streamco", "direction": "outflow"},
            status=ReviewStatus.resolved,
            resolution={"is_recurring": False, "resolved_by": "manual"},
        )
    )
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    (s,) = await _series(session)
    assert s.expected_cents == -999 and s.discretionary is True


async def test_split_bill_question_names_its_amount(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((3, -1000), (4, -1150), (5, -1320), (6, -1500)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Gymco", posted_on=date(2026, month, 15)
        )
    for d in (date(2026, 3, 2), date(2026, 4, 25), date(2026, 6, 1)):
        await make_txn(session, acct, amount_cents=-6000, merchant="Gymco", posted_on=d)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload["amount_cents"] == 1235
    assert item.question == "Is 'Gymco' ($12.35) a recurring bill?"


async def test_series_whose_txns_all_moved_ends(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    for t in (await session.execute(select(Transaction))).scalars():
        t.merchant = "Netflix Streaming"  # a rename this series wasn't carried through
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    old = next(s for s in await _series(session) if s.merchant == "Netflix")
    assert old.status == SeriesStatus.ended


async def test_split_supersedes_an_open_merchant_level_question(session: AsyncSession) -> None:
    await _seed_sub_and_orders(session)
    pending = recurring_cluster_item("Streamco", "outflow")  # asked before the split
    answered = recurring_cluster_item("Streamco", "outflow")
    answered.status = ReviewStatus.resolved
    answered.resolution = {"is_recurring": True, "resolved_by": "manual"}
    per_bill = recurring_cluster_item("Streamco", "outflow", 2600)
    session.add_all([pending, answered, per_bill])
    await session.commit()

    await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    await session.refresh(pending)
    await session.refresh(answered)
    await session.refresh(per_bill)
    assert pending.status == ReviewStatus.resolved
    assert pending.resolution is not None
    assert pending.resolution["resolved_by"] == "reconciliation"
    assert "superseded_reason" in pending.resolution
    assert answered.resolution == {"is_recurring": True, "resolved_by": "manual"}
    assert per_bill.status == ReviewStatus.open  # only merchant-level questions retire
    assert [s.expected_cents for s in await _series(session)] == [-999]


async def test_lone_bill_decision_follows_its_drifting_amount(session: AsyncSession) -> None:
    # a habit answer made when the bar's charges centred on $10 still covers it once they
    # centre on $17, and beats a newer merchant-level "bill" from the LLM
    acct = await make_account(session)
    for month, cents in ((1, -1000), (2, -1700), (3, -1700), (4, -2500), (5, -400), (6, -1700)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Acme Bar", posted_on=date(2026, month, 25)
        )
    habit = recurring_cluster_item("Acme Bar", "outflow", 1000)
    habit.status = ReviewStatus.resolved
    habit.resolution = {"is_recurring": True, "discretionary": True, "resolved_by": "manual"}
    session.add(habit)
    await session.flush()
    bill = recurring_cluster_item("Acme Bar", "outflow")
    bill.status = ReviewStatus.resolved
    bill.resolution = {"is_recurring": True, "resolved_by": "llm"}
    session.add(bill)
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    (s,) = await _series(session)
    assert s.discretionary is True


async def test_lone_steady_bill_ignores_a_decision_outside_its_charges(
    session: AsyncSession,
) -> None:
    # a decision about another bill the merchant once had says nothing about this one
    await _seed_monthly(session, (4, 5, 6))
    other = recurring_cluster_item("Netflix", "outflow", 2600)
    other.status = ReviewStatus.resolved
    other.resolution = {"is_recurring": False, "resolved_by": "manual"}
    session.add(other)
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert len(await _series(session)) == 1


# a payment plan charged from two cards under three spellings: a monthly installment
# (December and May paid from elsewhere), a larger deposit at each term start, and a fee
# beside the newest deposit
_INSTALLMENTS = (
    date(2025, 10, 15),
    date(2025, 11, 14),
    date(2026, 1, 15),
    date(2026, 2, 16),
    date(2026, 3, 16),
    date(2026, 4, 15),
    date(2026, 6, 15),
    date(2026, 7, 15),
    date(2026, 8, 14),
)
_DEPOSITS = (date(2025, 9, 2), date(2026, 1, 5), date(2026, 5, 4), date(2026, 9, 1))


async def _payment_plan(session: AsyncSession) -> list[Account]:
    cards = [await make_account(session, type=AccountType.credit) for _ in range(2)]
    for i, posted_on in enumerate(_INSTALLMENTS):
        raw = ("ACME UNIVERSITY CA", "ACME UNIVERSITYELMTON CA")[i % 2]
        await make_txn(
            session, cards[i % 2], amount_cents=-120000, description=raw, posted_on=posted_on
        )
    for cents, posted_on in [(-210000, d) for d in _DEPOSITS] + [(-14000, _DEPOSITS[-1])]:
        await make_txn(
            session,
            cards[0],
            amount_cents=cents,
            description="ACME UNIVERSITYRIVERTON CA",
            posted_on=posted_on,
        )
    await normalize_merchants(session, llm=None)
    await renormalize_merchants(session)
    return cards


async def test_term_deposits_stay_out_of_a_monthly_payment_plan(session: AsyncSession) -> None:
    """The deposit after the newest installment, before the next one is due, sits beside
    the schedule: the installment is the bill, and every deposit is ordinary spending."""
    await _payment_plan(session)
    today = date(2026, 9, 10)

    stats = await detect_recurring(session, llm=None, today=today)

    assert stats.new_series == 1 and stats.review == 0
    series = (await _series(session))[0]
    assert (series.merchant, series.cadence, series.expected_cents) == (
        "Acme University",
        Cadence.monthly,
        -120000,
    )
    assert series.status == SeriesStatus.active and series.discretionary is False
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert sorted(t.posted_on for t in txns if t.series_id == series.id) == list(_INSTALLMENTS)
    report = await power_report(session, today=today)
    assert [(line.merchant, line.monthly_cents) for line in report.fixed_costs] == [
        ("Acme University", 120000)
    ]
    assert report.spent_so_far_cents == 224000  # the newest deposit and its fee

    stats = await detect_recurring(session, llm=None, today=today)

    assert (stats.new_series, stats.updated, stats.review) == (0, 0, 0)


async def test_a_merchant_level_bill_ruling_never_tags_the_deposits(session: AsyncSession) -> None:
    """A "bill" answer to the merchant-level question covers every bill the merchant has,
    and the deposits are none of them: charges an earlier ruling tagged leave the series."""
    await _payment_plan(session)
    series = await make_series(
        session,
        merchant="Acme University",
        expected_cents=-120000,
        next_expected_on=date(2026, 9, 14),
    )
    for t in (await session.execute(select(Transaction))).scalars():
        t.series_id = series.id  # what the ruling tagged while the merchant was one bill
    ruling = recurring_cluster_item("Acme University", "outflow")
    ruling.status = ReviewStatus.resolved
    ruling.resolution = {"is_recurring": True, "resolved_by": "manual"}
    session.add(ruling)
    await session.commit()

    stats = await detect_recurring(session, llm=None, today=date(2026, 9, 10))

    assert (stats.new_series, stats.review) == (0, 0)
    assert [s.id for s in await _series(session)] == [series.id]
    assert series.expected_cents == -120000 and series.discretionary is False
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert sorted(t.posted_on for t in txns if t.series_id == series.id) == list(_INSTALLMENTS)


async def test_a_payment_plan_holds_across_a_month_with_only_a_deposit(
    session: AsyncSession,
) -> None:
    """September's installment was paid from an account that isn't linked, so only the
    deposit posted that month: October's installment continues the plan."""
    cards = await _payment_plan(session)
    await detect_recurring(session, llm=None, today=date(2026, 9, 10))
    await make_txn(
        session,
        cards[1],
        amount_cents=-120000,
        description="ACME UNIVERSITY CA",
        posted_on=date(2026, 10, 15),
    )
    await normalize_merchants(session, llm=None)
    await renormalize_merchants(session)

    today = date(2026, 10, 20)
    stats = await detect_recurring(session, llm=None, today=today)
    await emit_series_events(session, llm=None, today=today)
    # the next sync finds nothing new: the missed month is not announced twice
    again = await detect_recurring(session, llm=None, today=today)
    await emit_series_events(session, llm=None, today=today)

    assert (stats.new_series, stats.review) == (0, 0)
    assert (again.new_series, again.updated, again.review, again.merged) == (0, 0, 0, 0)
    (series,) = await _series(session)
    assert (series.cadence, series.expected_cents, series.status) == (
        Cadence.monthly,
        -120000,
        SeriesStatus.active,
    )
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert sorted(t.posted_on for t in txns if t.series_id == series.id) == [
        *_INSTALLMENTS,
        date(2026, 10, 15),
    ]
    items = (await session.execute(select(ReviewItem))).scalars().all()
    assert [i for i in items if i.status == ReviewStatus.open] == []
    events = (await session.execute(select(SeriesEvent))).scalars().all()
    # nothing posted around September's due date: one missed event, as for any bill
    assert [e.occurred_on for e in events if e.kind == EventKind.missed] == [date(2026, 9, 14)]


async def test_a_habit_busy_across_a_gap_forms_no_series(session: AsyncSession) -> None:
    """Three charges of one price 33 and 52 days apart look like a schedule with a skipped
    month, but the merchant kept charging at real size in between: spending, not a bill."""
    acct = await make_account(session)
    first = date(2026, 3, 1)
    for days, cents in ((0, -4000), (33, -4000), (85, -4000), (42, -9000), (50, -13000)):
        await make_txn(
            session,
            acct,
            amount_cents=cents,
            merchant="Acme Charging",
            posted_on=first + timedelta(days=days),
        )

    stats = await detect_recurring(session, llm=None, today=date(2026, 6, 1))

    assert stats.new_series == 0
    assert await _series(session) == []
