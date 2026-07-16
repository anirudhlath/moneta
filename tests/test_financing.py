from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, TransferLink
from moneta.views.financing import compute_obligations
from tests.factories import make_account, make_series, make_txn


async def _loan_setup(session: AsyncSession, promo: date | None = None) -> None:
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(
        session,
        type=AccountType.loan,
        name="Synchrony CarCare",
        balance_cents=-121500,
        promo_expires_on=promo,
    )
    series = await make_series(
        session, merchant="Synchrony Bank", expected_cents=-13500, next_expected_on=date(2026, 8, 5)
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
            series_id=series.id,
        )
        inn = await make_txn(
            session,
            loan,
            amount_cents=13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
        )
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
    await session.flush()


async def test_obligation_derived(session: AsyncSession) -> None:
    await _loan_setup(session)
    obs = await compute_obligations(session, today=date(2026, 7, 7))
    assert len(obs) == 1
    ob = obs[0]
    assert ob.account_name == "Synchrony CarCare"
    assert ob.balance_owed_cents == 121500
    assert ob.monthly_payment_cents == 13500
    assert ob.months_left == 9  # ceil(1215 / 135)
    assert ob.payoff_estimate == date(2027, 4, 3)  # today + 9*30 days
    assert ob.deferred_interest_risk is False


async def test_deferred_interest_risk(session: AsyncSession) -> None:
    await _loan_setup(session, promo=date(2026, 12, 31))
    obs = await compute_obligations(session, today=date(2026, 7, 7))
    assert obs[0].deferred_interest_risk is True  # payoff 2027-04 > promo 2026-12


async def test_loan_without_series_has_no_payment(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.loan, balance_cents=-50000)
    obs = await compute_obligations(session, today=date(2026, 7, 7))
    assert len(obs) == 1
    assert obs[0].monthly_payment_cents is None and obs[0].months_left is None
    assert obs[0].deferred_interest_risk is False


async def test_paid_off_loan_excluded(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.loan, balance_cents=0)
    assert await compute_obligations(session, today=date(2026, 7, 7)) == []
