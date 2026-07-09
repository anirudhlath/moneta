from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, Cadence, Direction, TransferLink
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
    assert report.monthly_income == Decimal("5416.67")  # 2500 * 26/12, cents-rounded
    assert report.total_fixed == Decimal("1815.99")
    assert report.spending_power == Decimal("3600.68")
    assert report.spent_so_far == Decimal("45")
    assert report.remaining == Decimal("3555.68")
    merchants = [line.merchant for line in report.fixed_costs]
    assert merchants == ["Landlord", "Netflix"]  # sorted by amount desc


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
    assert report.total_fixed == Decimal("0")  # CC payment series filtered out


async def test_stale_series_never_appears_in_fixed_costs(session: AsyncSession) -> None:
    acct = await make_account(session, type=AccountType.checking)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-4999, merchant="Dead Gym", posted_on=date(2025, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    report = await power_report(session, today=date(2026, 7, 8))
    assert report.fixed_costs == []
    assert report.total_fixed == Decimal(0)


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
    assert report.total_fixed == Decimal("135")


async def test_spent_ignores_foreign_currency_accounts(session: AsyncSession) -> None:
    usd = await make_account(session, type=AccountType.checking)
    eur = await make_account(session, type=AccountType.checking, currency="EUR")
    await make_txn(session, usd, amount_cents=-5000, posted_on=date(2026, 7, 3))
    await make_txn(session, eur, amount_cents=-7000, posted_on=date(2026, 7, 4))
    r = await power_report(session, today=date(2026, 7, 9))
    assert r.spent_so_far == Decimal("50.00")
