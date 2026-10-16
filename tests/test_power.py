from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    Cadence,
    Direction,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    SyncRun,
    TransferLink,
)
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.review import apply_resolution
from moneta.views.power import SeriesLine, UpcomingCharge, power_report
from tests.factories import make_account, make_series, make_txn


async def test_power_report_full_picture(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    payroll = await make_series(
        session,
        merchant="Acme Payroll",
        direction=Direction.inflow,
        cadence=Cadence.biweekly,
        expected_cents=250000,
    )
    netflix = await make_series(session)
    rent = await make_series(session, merchant="Landlord", expected_cents=-180000)
    # series-linked fixed-cost txn this month (must NOT double into spent_so_far)
    await make_txn(
        session,
        checking,
        amount_cents=-1599,
        merchant="Netflix",
        posted_on=date(2026, 7, 3),
        series_id=netflix.id,
    )
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        merchant="Acme Payroll",
        posted_on=date(2026, 7, 3),
        series_id=payroll.id,
    )
    _ = rent
    # discretionary spend this month
    await make_txn(
        session, checking, amount_cents=-4500, merchant="Restaurant", posted_on=date(2026, 7, 5)
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.month == "2026-07"
    assert report.monthly_income_cents == 541667  # 2500 * 26/12, cents-rounded
    assert report.total_fixed_cents == 181599
    assert report.spending_power_cents == 360068
    assert report.spent_so_far_cents == 4500
    assert report.remaining_cents == 355568
    merchants = [line.merchant for line in report.fixed_costs]
    assert merchants == ["Landlord", "Netflix"]  # sorted by amount desc
    income = [(line.merchant, line.cadence, line.monthly_cents) for line in report.income_sources]
    assert income == [("Acme Payroll", Cadence.biweekly, 541667)]


async def test_series_line_carries_expected_cents(session: AsyncSession) -> None:
    await make_series(
        session,
        merchant="Acme Payroll",
        direction=Direction.inflow,
        cadence=Cadence.biweekly,
        expected_cents=250000,
    )
    report = await power_report(session, today=date(2026, 7, 7))
    line = report.income_sources[0]
    assert line.expected_cents == 250000
    assert line.monthly_cents == 541667


async def test_credit_payment_series_excluded_from_fixed(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    credit = await make_account(session, type=AccountType.credit)
    cc_pay = await make_series(session, merchant="Chase Card Payment", expected_cents=-50000)
    out = await make_txn(
        session,
        checking,
        amount_cents=-50000,
        merchant="Chase Card Payment",
        posted_on=date(2026, 7, 5),
        series_id=cc_pay.id,
    )
    inn = await make_txn(
        session,
        credit,
        amount_cents=50000,
        merchant="Chase Card Payment",
        posted_on=date(2026, 7, 5),
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.total_fixed_cents == 0  # CC payment series filtered out


async def test_stale_series_never_appears_in_fixed_costs(session: AsyncSession) -> None:
    acct = await make_account(session, type=AccountType.checking)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-4999, merchant="Dead Gym", posted_on=date(2025, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    report = await power_report(session, today=date(2026, 7, 8))
    assert report.fixed_costs == []
    assert report.total_fixed_cents == 0


async def test_loan_payment_series_stays_in_fixed(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan)
    synchrony = await make_series(session, merchant="Synchrony Bank", expected_cents=-13500)
    out = await make_txn(
        session,
        checking,
        amount_cents=-13500,
        merchant="Synchrony Bank",
        posted_on=date(2026, 7, 5),
        series_id=synchrony.id,
    )
    inn = await make_txn(
        session, loan, amount_cents=13500, merchant="Synchrony Bank", posted_on=date(2026, 7, 5)
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.total_fixed_cents == 13500


async def test_loan_payment_lines_in_fixed_costs(session: AsyncSession) -> None:
    """Loan-linked payments with no recurring series of their own must still
    surface as derived fixed-cost lines, one per loan account."""
    checking = await make_account(session, type=AccountType.checking)
    loan_a = await make_account(session, type=AccountType.loan, name="Car Loan")
    loan_b = await make_account(session, type=AccountType.loan, name="Furniture Loan")
    out_a = await make_txn(
        session,
        checking,
        amount_cents=-13500,
        merchant="Synchrony Bank",
        posted_on=date(2026, 7, 5),
    )
    inn_a = await make_txn(
        session, loan_a, amount_cents=13500, merchant="Synchrony Bank", posted_on=date(2026, 7, 5)
    )
    session.add(
        TransferLink(outflow_id=out_a.id, inflow_id=inn_a.id, confidence=1.0, method="rule")
    )
    out_b = await make_txn(
        session,
        checking,
        amount_cents=-20000,
        merchant="Synchrony Bank",
        posted_on=date(2026, 7, 6),
    )
    inn_b = await make_txn(
        session, loan_b, amount_cents=20000, merchant="Synchrony Bank", posted_on=date(2026, 7, 6)
    )
    session.add(
        TransferLink(outflow_id=out_b.id, inflow_id=inn_b.id, confidence=1.0, method="rule")
    )
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    merchants = {line.merchant for line in report.fixed_costs}
    assert merchants == {"Car Loan — payment", "Furniture Loan — payment"}
    assert report.total_fixed_cents == 33500


async def test_financing_mode_payments_are_fixed_costs_not_cc_excluded(
    session: AsyncSession,
) -> None:
    """A financing-mode credit account (type stays `credit`) is loan-like, so its
    linked payment series must count as a fixed cost, not get filtered out as a
    plain credit-card payment (whose purchases are already counted as spend)."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(
        session,
        type=AccountType.credit,
        financing_mode=True,
        name="Synchrony Store Card",
    )
    payment = await make_series(
        session, merchant="Synchrony Store Card Payment", expected_cents=-30000
    )
    out = await make_txn(
        session,
        checking,
        amount_cents=-30000,
        merchant="Synchrony Store Card Payment",
        posted_on=date(2026, 7, 5),
        series_id=payment.id,
    )
    inn = await make_txn(
        session,
        card,
        amount_cents=30000,
        merchant="Synchrony Store Card Payment",
        posted_on=date(2026, 7, 5),
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.total_fixed_cents == 30000
    assert [line.merchant for line in report.fixed_costs] == ["Synchrony Store Card Payment"]


async def test_spent_ignores_foreign_currency_accounts(session: AsyncSession) -> None:
    usd = await make_account(session, type=AccountType.checking)
    eur = await make_account(session, type=AccountType.checking, currency="EUR")
    await make_txn(session, usd, amount_cents=-5000, posted_on=date(2026, 7, 3))
    await make_txn(session, eur, amount_cents=-7000, posted_on=date(2026, 7, 4))
    r = await power_report(session, today=date(2026, 7, 9))
    assert r.spent_so_far_cents == 5000


async def test_ended_series_txns_count_as_spent(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    dead_gym = await make_series(
        session, merchant="Dead Gym", expected_cents=-4999, status=SeriesStatus.ended
    )
    await make_txn(
        session,
        checking,
        amount_cents=-4999,
        merchant="Dead Gym",
        posted_on=date(2026, 7, 5),
        series_id=dead_gym.id,
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.spent_so_far_cents == 4999
    assert report.remaining_cents == report.spending_power_cents - 4999


async def test_discretionary_series_excluded_from_fixed_and_counted_as_spend(
    session: AsyncSession,
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    dining = await make_series(
        session, merchant="Dining Out", expected_cents=-3886, discretionary=True
    )
    await make_txn(
        session,
        checking,
        amount_cents=-3886,
        merchant="Dining Out",
        posted_on=date(2026, 7, 5),
        series_id=dining.id,
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.total_fixed_cents == 0
    assert report.spent_so_far_cents == 3886


async def test_discretionary_inflow_not_income(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    reimbursement = await make_series(
        session,
        merchant="Expense Reimbursement",
        direction=Direction.inflow,
        expected_cents=20000,
        discretionary=True,
    )
    await make_txn(
        session,
        checking,
        amount_cents=20000,
        merchant="Expense Reimbursement",
        posted_on=date(2026, 7, 5),
        series_id=reimbursement.id,
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.income_sources == []
    assert report.monthly_income_cents == 0


async def test_power_report_empty_db_is_zeroed(session: AsyncSession) -> None:
    """No accounts, no series, no transactions — power_report must return zeroed
    values (no exception from sum() of an empty sequence or a None division)."""
    report = await power_report(session, today=date(2026, 7, 15))
    assert report.monthly_income_cents == 0
    assert report.income_sources == []
    assert report.fixed_costs == []
    assert report.total_fixed_cents == 0
    assert report.spending_power_cents == 0
    assert report.spent_so_far_cents == 0
    assert report.remaining_cents == 0
    assert report.per_day_remaining_cents == 0
    assert report.upcoming == []
    assert report.data_as_of is None


async def test_per_day_remaining_mid_month(session: AsyncSession) -> None:
    today = date(2026, 7, 15)
    last_day = monthrange(today.year, today.month)[1]
    expected_days_left = (date(today.year, today.month, last_day) - today).days + 1
    report = await power_report(session, today=today)
    assert report.days_left == expected_days_left
    assert report.per_day_remaining_cents == round(report.remaining_cents / expected_days_left)


async def test_per_day_remaining_month_end_no_division_by_zero(session: AsyncSession) -> None:
    today = date(2026, 7, 31)
    report = await power_report(session, today=today)
    assert report.days_left == 1
    assert report.per_day_remaining_cents == report.remaining_cents


async def test_per_day_remaining_negative_when_remaining_negative(session: AsyncSession) -> None:
    await make_series(session, merchant="Landlord", expected_cents=-99999999)
    today = date(2026, 7, 15)
    report = await power_report(session, today=today)
    assert report.remaining_cents < 0
    assert report.per_day_remaining_cents < 0
    last_day = monthrange(today.year, today.month)[1]
    expected_days_left = (date(today.year, today.month, last_day) - today).days + 1
    assert report.per_day_remaining_cents == round(report.remaining_cents / expected_days_left)


async def test_upcoming_includes_series_inside_window_excludes_outside(
    session: AsyncSession,
) -> None:
    await make_series(
        session, merchant="Rent", expected_cents=-140000, next_expected_on=date(2026, 7, 15)
    )
    await make_series(
        session, merchant="Insurance", expected_cents=-30000, next_expected_on=date(2026, 8, 1)
    )
    # boundaries: due today (not yet charged) and due on the month's last day are both
    # upcoming — the window is [today, month_end]
    await make_series(
        session, merchant="Today Charge", expected_cents=-500, next_expected_on=date(2026, 7, 7)
    )
    await make_series(
        session, merchant="Month End", expected_cents=-2000, next_expected_on=date(2026, 7, 31)
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.upcoming == [
        UpcomingCharge(merchant="Today Charge", expected_on=date(2026, 7, 7), expected_cents=500),
        UpcomingCharge(merchant="Rent", expected_on=date(2026, 7, 15), expected_cents=140000),
        UpcomingCharge(merchant="Month End", expected_on=date(2026, 7, 31), expected_cents=2000),
    ]


async def test_bill_charged_today_is_not_also_upcoming(session: AsyncSession) -> None:
    """Paid today = counted once, as a fixed cost: detection moves the bill's next date
    a period forward, so it never also shows as still to come."""
    checking = await make_account(session, type=AccountType.checking)
    for month in (5, 6, 7):
        await make_txn(
            session,
            checking,
            amount_cents=-5000,
            merchant="Corner Gym",
            posted_on=date(2026, month, 7),
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 7))
    report = await power_report(session, today=date(2026, 7, 7))
    assert [line.merchant for line in report.fixed_costs] == ["Corner Gym"]
    assert report.upcoming == []


async def test_upcoming_excludes_discretionary_and_cc_payment_series(
    session: AsyncSession,
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    credit = await make_account(session, type=AccountType.credit)
    await make_series(
        session,
        merchant="Dining Out",
        expected_cents=-3886,
        discretionary=True,
        next_expected_on=date(2026, 7, 20),
    )
    cc_pay = await make_series(
        session,
        merchant="Chase Card Payment",
        expected_cents=-50000,
        next_expected_on=date(2026, 7, 20),
    )
    out = await make_txn(
        session,
        checking,
        amount_cents=-50000,
        merchant="Chase Card Payment",
        posted_on=date(2026, 7, 5),
        series_id=cc_pay.id,
    )
    inn = await make_txn(
        session,
        credit,
        amount_cents=50000,
        merchant="Chase Card Payment",
        posted_on=date(2026, 7, 5),
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.upcoming == []


async def test_upcoming_includes_loan_payment_projected_date(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan, name="Car Loan")
    out = await make_txn(
        session,
        checking,
        amount_cents=-13500,
        merchant="Synchrony Bank",
        posted_on=date(2026, 6, 15),
    )
    inn = await make_txn(
        session, loan, amount_cents=13500, merchant="Synchrony Bank", posted_on=date(2026, 6, 15)
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.upcoming == [
        UpcomingCharge(
            merchant="Car Loan — payment", expected_on=date(2026, 7, 15), expected_cents=13500
        )
    ]


async def test_upcoming_loan_payment_due_today_but_not_one_paid_today(
    session: AsyncSession,
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    due = await make_account(session, type=AccountType.loan, name="Acme Car Loan")
    paid = await make_account(session, type=AccountType.loan, name="Acme Boat Loan")
    for loan, day in ((due, date(2026, 6, 7)), (paid, date(2026, 6, 7)), (paid, date(2026, 7, 7))):
        out = await make_txn(
            session, checking, amount_cents=-13500, merchant="Acme Bank", posted_on=day
        )
        inn = await make_txn(session, loan, amount_cents=13500, merchant="Acme Bank", posted_on=day)
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.upcoming == [
        UpcomingCharge(
            merchant="Acme Car Loan — payment", expected_on=date(2026, 7, 7), expected_cents=13500
        )
    ]


async def test_upcoming_sorted_by_expected_on(session: AsyncSession) -> None:
    await make_series(
        session, merchant="Rent", expected_cents=-140000, next_expected_on=date(2026, 7, 28)
    )
    await make_series(
        session, merchant="Netflix", expected_cents=-1599, next_expected_on=date(2026, 7, 10)
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert [u.merchant for u in report.upcoming] == ["Netflix", "Rent"]


async def test_data_as_of_is_none_with_no_sync_run(session: AsyncSession) -> None:
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.data_as_of is None


async def test_data_as_of_ignores_failed_runs(session: AsyncSession) -> None:
    session.add(SyncRun(success=False, finished_at=datetime(2026, 7, 6, 9, 0)))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.data_as_of is None


async def test_data_as_of_reports_newest_successful_run(session: AsyncSession) -> None:
    session.add(SyncRun(success=True, finished_at=datetime(2026, 7, 5, 9, 0)))
    session.add(SyncRun(success=True, finished_at=datetime(2026, 7, 6, 10, 0)))
    session.add(SyncRun(success=False, finished_at=datetime(2026, 7, 7, 11, 0)))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.data_as_of == datetime(2026, 7, 6, 10, 0)


async def _card_paid_monthly(
    session: AsyncSession, series_id: int | None = None, **card: Any
) -> Account:
    """A credit card owing $900.00, paid $100.00 from checking on May, June and July 5."""
    checking = await make_account(session, type=AccountType.checking)
    account = await make_account(
        session, type=AccountType.credit, name="Acme Store Card", balance_cents=-90000, **card
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-10000,
            merchant="Acme Card Payment",
            posted_on=date(2026, month, 5),
            series_id=series_id,
        )
        inn = await make_txn(
            session,
            account,
            amount_cents=10000,
            merchant="Acme Card Payment",
            posted_on=date(2026, month, 5),
        )
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
    await session.flush()
    return account


def _financing_question(account: Account) -> ReviewItem:
    return ReviewItem(
        kind=ReviewKind.financing_account,
        question="'Acme Store Card' looks like promo financing being paid down?",
        payload={"account_id": account.id},
    )


async def _second_provider_copy(session: AsyncSession, card: Account) -> Account:
    """The same card as a second provider reports it: an alias of the first record."""
    return await make_account(
        session,
        type=AccountType.credit,
        name="Acme Store Card",
        balance_cents=-90000,
        canonical_account_id=card.id,
        source="second",
    )


_CARD_PAYMENT_LINE = SeriesLine(
    merchant="Acme Store Card — payment",
    cadence=Cadence.monthly,
    monthly_cents=10000,
    expected_cents=10000,
)


@pytest.mark.parametrize("state", ["confirmed", "open question"])
async def test_open_financing_question_counts_payments_as_fixed(
    session: AsyncSession, state: str
) -> None:
    """No one has answered yet (the LLM never does): the card counts exactly as a
    confirmed financing card does."""
    card = await _card_paid_monthly(session, financing_mode=state == "confirmed")
    if state == "open question":
        session.add(_financing_question(card))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == [_CARD_PAYMENT_LINE]
    assert report.total_fixed_cents == 10000


async def test_resolving_not_financing_makes_it_a_revolving_card_again(
    session: AsyncSession,
) -> None:
    card = await _card_paid_monthly(session)
    item = _financing_question(card)
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"financing": False})
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == []  # a plain card's payments are never fixed costs
    assert report.spent_so_far_cents == 0  # nor spend: a card payment is a transfer


async def test_open_financing_question_with_old_payment_series_counts_once(
    session: AsyncSession,
) -> None:
    """A payment series formed before links excluded it stays the one line — no derived
    payment line on top of it."""
    series = await make_series(session, merchant="Acme Card Payment", expected_cents=-10000)
    card = await _card_paid_monthly(session, series_id=series.id)
    session.add(_financing_question(card))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert [line.merchant for line in report.fixed_costs] == ["Acme Card Payment"]
    assert report.total_fixed_cents == 10000


async def test_financing_ruling_beats_an_open_question_on_an_alias(
    session: AsyncSession,
) -> None:
    """Ruled 'not financing', then the same card reappears from a second provider with
    its own open question: the human's ruling stands."""
    card = await _card_paid_monthly(session)
    alias = await _second_provider_copy(session, card)
    ruled = _financing_question(card)
    session.add(ruled)
    await session.flush()
    await apply_resolution(session, ruled, {"financing": False})
    session.add(_financing_question(alias))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == []


async def test_open_financing_question_on_an_alias_makes_the_card_financing(
    session: AsyncSession,
) -> None:
    """The question was opened against the second provider's copy of the card; the
    card itself (the logical account the payments land in) counts as financing."""
    card = await _card_paid_monthly(session)
    alias = await _second_provider_copy(session, card)
    session.add(_financing_question(alias))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == [_CARD_PAYMENT_LINE]
    assert report.total_fixed_cents == 10000


async def test_financing_ruling_on_an_alias_beats_an_open_question_on_the_card(
    session: AsyncSession,
) -> None:
    """'Not financing' was answered for the second provider's copy before the two were
    linked; the card's own question is still open, and the ruling stands."""
    card = await _card_paid_monthly(session)
    alias = await _second_provider_copy(session, card)
    session.add(_financing_question(card))
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="'Acme Store Card' looks like promo financing being paid down?",
            payload={"account_id": alias.id},
            status=ReviewStatus.resolved,
            resolution={"financing": False, "resolved_by": "manual"},
        )
    )
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == []


@pytest.mark.parametrize(
    ("state", "fixed", "spent"),
    [
        ("financing mode", [_CARD_PAYMENT_LINE], 0),
        ("open question", [_CARD_PAYMENT_LINE], 0),
        ("plain card", [], 100000),
    ],
)
async def test_financed_purchase_is_not_also_spend(
    session: AsyncSession, state: str, fixed: list[SeriesLine], spent: int
) -> None:
    """A $1,000.00 financed purchase is paid off by the card's $100.00/mo payments, and
    those are the fixed cost: counting the purchase as spend too would count it twice.
    A plain card's purchase is spend, and its payment is a transfer."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(
        session,
        type=AccountType.credit,
        name="Acme Store Card",
        balance_cents=-90000,
        financing_mode=state == "financing mode",
    )
    if state == "open question":
        session.add(_financing_question(card))
    await make_txn(
        session, card, amount_cents=-100000, merchant="Acme Furniture", posted_on=date(2026, 7, 2)
    )
    out = await make_txn(
        session,
        checking,
        amount_cents=-10000,
        merchant="Acme Card Payment",
        posted_on=date(2026, 7, 5),
    )
    inn = await make_txn(
        session, card, amount_cents=10000, merchant="Acme Card Payment", posted_on=date(2026, 7, 5)
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == fixed
    assert report.spent_so_far_cents == spent


async def test_power_reports_quiet_feeds(session: AsyncSession) -> None:
    today = date(2026, 7, 9)
    card = await make_account(session, name="Acme Card", mask="1234", type=AccountType.credit)
    for days_ago in range(14, 201):
        await make_txn(session, card, posted_on=today - timedelta(days=days_ago))
    report = await power_report(session, today=today)
    assert [(s.account_id, s.newest_posted_on) for s in report.stale_accounts] == [
        (card.id, date(2026, 6, 25))
    ]


async def test_power_reports_no_quiet_feeds_on_an_empty_db(session: AsyncSession) -> None:
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.stale_accounts == []
