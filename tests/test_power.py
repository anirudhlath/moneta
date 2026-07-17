from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, Cadence, Direction, SeriesStatus, TransferLink
from moneta.pipelines.recurring import detect_recurring
from moneta.views.power import power_report
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
