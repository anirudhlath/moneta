from datetime import date

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.cadence import month_bounds
from moneta.models import (
    Account,
    AccountType,
    Direction,
    ReviewStatus,
    SeriesStatus,
    TransferLink,
    recurring_cluster_item,
)
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.review import apply_resolution
from moneta.views.cashflow import accrual_by_month, accrual_inflows, accrual_spend, cash_out
from tests.factories import make_account, make_series, make_txn


async def _cc_purchase_and_payment(session: AsyncSession) -> tuple[Account, Account]:
    """Checking+credit accounts: one credit purchase, and a checking->credit CC payment link."""
    checking = await make_account(session, type=AccountType.checking)
    credit = await make_account(session, type=AccountType.credit)
    await make_txn(
        session, credit, amount_cents=-8000, posted_on=date(2026, 7, 2), description="RESTAURANT"
    )
    out = await make_txn(
        session, checking, amount_cents=-8000, posted_on=date(2026, 7, 5), description="CC PAYMENT"
    )
    inn = await make_txn(
        session,
        credit,
        amount_cents=8000,
        posted_on=date(2026, 7, 5),
        description="PAYMENT THANK YOU",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    return checking, credit


async def test_accrual_counts_cc_purchases_not_payments(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == 8000


async def test_cash_out_counts_cc_payment_not_purchase(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    assert await cash_out(session, date(2026, 7, 1), date(2026, 7, 31)) == 8000


async def test_internal_moves_count_nowhere(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session, checking, amount_cents=-10000, posted_on=date(2026, 7, 3), description="TO SAVINGS"
    )
    inn = await make_txn(
        session,
        savings,
        amount_cents=10000,
        posted_on=date(2026, 7, 3),
        description="FROM CHECKING",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == 0
    assert await cash_out(session, date(2026, 7, 1), date(2026, 7, 31)) == 0


async def test_cash_out_counts_outflow_to_loan_payment(session: AsyncSession) -> None:
    """Only liquid->liquid moves are excluded from cash_out; an outflow paying a
    loan account (inflow_account_type == loan) is a real cash cost and counts."""
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan)
    out = await make_txn(
        session,
        checking,
        amount_cents=-13500,
        posted_on=date(2026, 7, 5),
        description="SYNCHRONY BANK PAYMENT",
    )
    inn = await make_txn(
        session,
        loan,
        amount_cents=13500,
        posted_on=date(2026, 7, 5),
        description="SYNCHRONY BANK PAYMENT",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    assert await cash_out(session, date(2026, 7, 1), date(2026, 7, 31)) == 13500


async def test_cashflow_endpoint_returns_accrual_and_cash_out(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _cc_purchase_and_payment(session)
    await session.commit()

    app = create_app(sessionmaker, adapters=[], llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/cashflow", params={"start": "2026-07-01", "end": "2026-07-31"})

    assert r.status_code == 200
    body = r.json()
    assert body["start"] == "2026-07-01"
    assert body["end"] == "2026-07-31"
    assert body["accrual_cents"] == 8000  # the RESTAURANT purchase
    assert body["cash_out_cents"] == 8000  # the CC PAYMENT, not the purchase


async def test_loan_account_purchase_not_accrual(session: AsyncSession) -> None:
    loan = await make_account(session, type=AccountType.loan)
    await make_txn(
        session,
        loan,
        amount_cents=-120000,
        posted_on=date(2026, 7, 2),
        description="FURNITURE STORE FINANCED PURCHASE",
    )
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == 0


async def test_accrual_inflows_counts_every_unlinked_inflow(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 2),
        description="ACME CORP PAYROLL",
    )
    assert await accrual_inflows(session, date(2026, 7, 1), date(2026, 7, 31)) == 250000


async def test_accrual_inflows_excludes_linked_inflow(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    # the only positive-amount txn in this fixture is the CC payment's inflow leg
    # (credit account, +8000, transfer-linked) — it must not count as income.
    assert await accrual_inflows(session, date(2026, 7, 1), date(2026, 7, 31)) == 0


async def _payroll_series(session: AsyncSession, **kw: object) -> int:
    series = await make_series(
        session,
        merchant="Acme Payroll",
        direction=Direction.inflow,
        expected_cents=250000,
        **kw,
    )
    return series.id


async def test_accrual_by_month_buckets_multiple_months(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-5000, posted_on=date(2026, 7, 10), description="GROCERY"
    )
    await make_txn(
        session,
        checking,
        amount_cents=200000,
        posted_on=date(2026, 6, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-4000, posted_on=date(2026, 6, 20), description="GROCERY"
    )
    rows = await accrual_by_month(session, months=2, today=date(2026, 7, 20))
    assert rows == [
        ("2026-07", 250000, 0, 5000),
        ("2026-06", 200000, 0, 4000),
    ]


async def test_history_income_is_detected_income_only(session: AsyncSession) -> None:
    """#19: a friend's Zelle inflow is another inflow, not income; a paycheck tagged
    to a payroll series is income."""
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 1),
        description="ACME PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 3),
        description="Zelle payment from JANE DOE 1234",
    )
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 250000, 30000, 0)]
    assert (rows[0].income_cents, rows[0].other_inflows_cents) == (250000, 30000)


async def test_card_refund_is_another_inflow_not_income(session: AsyncSession) -> None:
    credit = await make_account(session, type=AccountType.credit)
    await make_txn(
        session, credit, amount_cents=-5000, posted_on=date(2026, 7, 2), description="ACME STORE"
    )
    await make_txn(
        session,
        credit,
        amount_cents=5000,
        posted_on=date(2026, 7, 9),
        description="ACME STORE REFUND",
    )
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 5000, 5000)]


async def test_ended_paycheck_series_counts_in_the_months_it_paid(session: AsyncSession) -> None:
    """A former employer's series has ended; its paychecks were income in their months."""
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session, status=SeriesStatus.ended)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 5, 1),
        description="ACME PAYROLL",
        series_id=payroll,
    )
    rows = await accrual_by_month(session, months=3, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 0, 0), ("2026-06", 0, 0, 0), ("2026-05", 250000, 0, 0)]


async def test_discretionary_inflow_series_is_not_income(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    habit = await _payroll_series(session, discretionary=True)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 1),
        description="ACME PAYROLL",
        series_id=habit,
    )
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 250000, 0)]


async def test_not_recurring_answer_takes_its_inflows_out_of_income(
    session: AsyncSession,
) -> None:
    """#19: a person's monthly repayments formed an inflow series; the owner answered
    "not recurring". The bill then owns no charges, so history reports the money as
    other inflows, never income, right after the answer and after the next detection."""
    checking = await make_account(session, type=AccountType.checking)
    series = await make_series(
        session, merchant="Jane Doe", direction=Direction.inflow, expected_cents=30000
    )
    for posted_on in (date(2026, 5, 3), date(2026, 6, 3), date(2026, 7, 3)):
        await make_txn(
            session,
            checking,
            amount_cents=30000,
            posted_on=posted_on,
            description="Jane Doe",
            merchant="Jane Doe",
            series_id=series.id,
        )
    before = await accrual_by_month(session, months=3, today=date(2026, 7, 20))
    assert [row.income_cents for row in before] == [30000, 30000, 30000]

    item = recurring_cluster_item("Jane Doe", "inflow")
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_recurring": False}, resolved_by="manual")

    expected = [("2026-07", 0, 30000, 0), ("2026-06", 0, 30000, 0), ("2026-05", 0, 30000, 0)]
    assert series.status == SeriesStatus.ended
    assert await accrual_by_month(session, months=3, today=date(2026, 7, 20)) == expected

    await detect_recurring(session, None, date(2026, 7, 20))
    assert series.status == SeriesStatus.ended
    assert await accrual_by_month(session, months=3, today=date(2026, 7, 20)) == expected


async def test_detection_untags_a_bill_already_answered_not_recurring(
    session: AsyncSession,
) -> None:
    """A database that answered "not recurring" before untagging existed is repaired by
    the next detection run."""
    checking = await make_account(session, type=AccountType.checking)
    series = await make_series(
        session,
        merchant="Jane Doe",
        direction=Direction.inflow,
        expected_cents=30000,
        status=SeriesStatus.ended,
    )
    for posted_on in (date(2026, 5, 3), date(2026, 6, 3), date(2026, 7, 3)):
        await make_txn(
            session,
            checking,
            amount_cents=30000,
            posted_on=posted_on,
            description="Jane Doe",
            merchant="Jane Doe",
            series_id=series.id,
        )
    item = recurring_cluster_item("Jane Doe", "inflow")
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": False, "resolved_by": "manual"}
    session.add(item)
    await session.flush()
    await detect_recurring(session, None, date(2026, 7, 20))
    rows = await accrual_by_month(session, months=3, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 30000, 0), ("2026-06", 0, 30000, 0), ("2026-05", 0, 30000, 0)]
    assert series.status == SeriesStatus.ended


async def test_transfer_linked_inflow_of_an_income_series_is_neither_income_nor_other(
    session: AsyncSession,
) -> None:
    """A leg of a transfer link is never counted, whatever series tag it carries."""
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    payroll = await _payroll_series(session)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 1),
        description="ACME PAYROLL",
        series_id=payroll,
    )
    out = await make_txn(
        session, savings, amount_cents=-30000, posted_on=date(2026, 7, 3), description="TO CHECKING"
    )
    inn = await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 3),
        description="FROM SAVINGS",
        series_id=payroll,
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 250000, 0, 0)]


async def test_accrual_by_month_excludes_linked_txns(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    # same fixture as test_accrual_counts_cc_purchases_not_payments/
    # test_accrual_inflows_excludes_linked_inflow: RESTAURANT counts as spend,
    # the CC PAYMENT's linked legs count nowhere.
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 0, 8000)]


async def test_accrual_by_month_year_boundary_and_equivalence(session: AsyncSession) -> None:
    """3-month trailing window crossing the Dec/Jan year boundary: buckets land in the
    right calendar month (label year rolls back correctly), and each bucket agrees
    exactly with accrual_inflows/accrual_spend called directly over that month's
    [month_start, month_end] window — the single-SELECT batching in accrual_by_month
    must not diverge from the per-month accessors it's meant to replace."""
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session)
    # Nov 2025
    await make_txn(
        session,
        checking,
        amount_cents=200000,
        posted_on=date(2025, 11, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-3000, posted_on=date(2025, 11, 20), description="GROCERY"
    )
    # Dec 2025
    await make_txn(
        session,
        checking,
        amount_cents=210000,
        posted_on=date(2025, 12, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=1500, posted_on=date(2025, 12, 9), description="REFUND"
    )
    await make_txn(
        session, checking, amount_cents=-4500, posted_on=date(2025, 12, 24), description="GIFTS"
    )
    # Jan 2026 — the "today" month, still in progress
    await make_txn(
        session,
        checking,
        amount_cents=220000,
        posted_on=date(2026, 1, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-6000, posted_on=date(2026, 1, 10), description="RENT"
    )

    today = date(2026, 1, 15)
    rows = await accrual_by_month(session, months=3, today=today)
    assert rows == [
        ("2026-01", 220000, 0, 6000),
        ("2025-12", 210000, 1500, 4500),
        ("2025-11", 200000, 0, 3000),
    ]

    for label, income_cents, other_inflows_cents, spend_cents in rows:
        year_s, month_s = label.split("-")
        start, end = month_bounds(date(int(year_s), int(month_s), 1))
        assert await accrual_inflows(session, start, end) == income_cents + other_inflows_cents
        assert await accrual_spend(session, start, end) == spend_cents
