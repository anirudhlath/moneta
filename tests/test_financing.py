from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, Transaction, TransferLink
from moneta.views.financing import compute_obligations
from tests.factories import make_account, make_txn


async def _link(session: AsyncSession, out_txn: Transaction, in_txn: Transaction) -> None:
    session.add(
        TransferLink(outflow_id=out_txn.id, inflow_id=in_txn.id, confidence=1.0, method="rule")
    )


async def test_merged_descriptors_two_loans_two_obligations(session: AsyncSession) -> None:
    """Two loans paid from the same checking account with an identical bank
    descriptor but different amounts/dates must resolve to two distinct
    per-account payments, not one merged (or missing) figure."""
    checking = await make_account(session, type=AccountType.checking)
    loan_a = await make_account(
        session, type=AccountType.loan, name="Synchrony CarCare", balance_cents=-19332
    )
    loan_b = await make_account(
        session, type=AccountType.loan, name="Synchrony Furniture", balance_cents=-31929
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-6444,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(
            session,
            loan_a,
            amount_cents=6444,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 5),
        )
        await _link(session, out, inn)
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-10643,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 10),
        )
        inn = await make_txn(
            session,
            loan_b,
            amount_cents=10643,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 10),
        )
        await _link(session, out, inn)
    await session.flush()

    obs = await compute_obligations(session, today=date(2026, 7, 7))
    assert len(obs) == 2
    by_id = {ob.account_id: ob for ob in obs}
    assert by_id[loan_a.id].monthly_payment_cents == 6444
    assert by_id[loan_a.id].balance_owed_cents == 19332
    assert by_id[loan_b.id].monthly_payment_cents == 10643
    assert by_id[loan_b.id].balance_owed_cents == 31929


async def test_two_payments_monthly_fallback(session: AsyncSession) -> None:
    """Only two linked payments can't match a cadence run (needs >=3); the
    derivation must still fall back to monthly rather than leaving `?`."""
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(
        session, type=AccountType.loan, name="Synchrony CarCare", balance_cents=-50000
    )
    for month in (6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(
            session,
            loan,
            amount_cents=13500,
            merchant="Synchrony Bank",
            posted_on=date(2026, month, 5),
        )
        await _link(session, out, inn)
    await session.flush()

    obs = await compute_obligations(session, today=date(2026, 7, 7))
    assert len(obs) == 1
    assert obs[0].monthly_payment_cents == 13500
    assert obs[0].months_left is not None


async def test_financing_mode_account_gets_obligation(session: AsyncSession) -> None:
    """A financing-mode credit card (type stays `credit`) must still get an
    obligations row, sourced from its linked payments, with deferred-interest
    risk computed against its promo expiry."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(
        session,
        type=AccountType.credit,
        financing_mode=True,
        name="Synchrony Store Card",
        balance_cents=-90000,
        promo_expires_on=date(2026, 9, 1),
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-30000,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(
            session,
            card,
            amount_cents=30000,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 5),
        )
        await _link(session, out, inn)
    await session.flush()

    obs = await compute_obligations(session, today=date(2026, 7, 7))
    assert len(obs) == 1
    ob = obs[0]
    assert ob.account_id == card.id
    assert ob.monthly_payment_cents == 30000
    assert ob.months_left == 3
    assert ob.payoff_estimate == date(2026, 10, 5)  # today + 3*30 days
    assert ob.deferred_interest_risk is True  # payoff 2026-10-05 > promo 2026-09-01


async def test_loan_without_links_has_no_payment(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.loan, balance_cents=-50000)
    obs = await compute_obligations(session, today=date(2026, 7, 7))
    assert len(obs) == 1
    assert obs[0].monthly_payment_cents is None and obs[0].months_left is None
    assert obs[0].deferred_interest_risk is False


async def test_paid_off_loan_excluded(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.loan, balance_cents=0)
    assert await compute_obligations(session, today=date(2026, 7, 7)) == []
