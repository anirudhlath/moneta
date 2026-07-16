from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, Cadence, TransferLink
from moneta.queries import ClassifiedLink, classified_links, loan_payment_stats
from tests.factories import make_account, make_txn


def _link(
    *,
    outflow_id: int = 1,
    inflow_id: int = 2,
    outflow_account_id: int = 1,
    inflow_account_id: int,
    outflow_amount_cents: int,
    outflow_posted_on: date,
    inflow_is_loan_like: bool,
    outflow_account_type: AccountType = AccountType.checking,
    inflow_account_type: AccountType = AccountType.loan,
    outflow_series_id: int | None = None,
) -> ClassifiedLink:
    """Hand-built ClassifiedLink for pure-value tests — no DB needed (frozen dataclass)."""
    return ClassifiedLink(
        outflow_id=outflow_id,
        inflow_id=inflow_id,
        outflow_account_id=outflow_account_id,
        inflow_account_id=inflow_account_id,
        outflow_account_type=outflow_account_type,
        inflow_account_type=inflow_account_type,
        outflow_series_id=outflow_series_id,
        outflow_posted_on=outflow_posted_on,
        outflow_amount_cents=outflow_amount_cents,
        inflow_is_loan_like=inflow_is_loan_like,
    )


def test_loan_payment_stats_groups_by_inflow_account() -> None:
    links = [
        _link(
            inflow_account_id=10,
            outflow_amount_cents=-6444,
            outflow_posted_on=date(2026, month, 1),
            inflow_is_loan_like=True,
        )
        for month in (5, 6, 7)
    ] + [
        _link(
            inflow_account_id=11,
            outflow_amount_cents=-10643,
            outflow_posted_on=date(2026, month, 15),
            inflow_is_loan_like=True,
        )
        for month in (5, 6, 7)
    ]

    stats = loan_payment_stats(links)

    assert set(stats) == {10, 11}
    assert stats[10].expected_cents == -6444
    assert stats[10].cadence == Cadence.monthly
    assert stats[11].expected_cents == -10643
    assert stats[11].cadence == Cadence.monthly


def test_loan_payment_stats_two_payments_falls_back_monthly() -> None:
    links = [
        _link(
            inflow_account_id=20,
            outflow_amount_cents=-19900,
            outflow_posted_on=date(2026, 5, 1),
            inflow_is_loan_like=True,
        ),
        _link(
            inflow_account_id=20,
            outflow_amount_cents=-20100,
            outflow_posted_on=date(2026, 5, 31),
            inflow_is_loan_like=True,
        ),
    ]

    stats = loan_payment_stats(links)

    assert stats[20].cadence == Cadence.monthly
    assert stats[20].expected_cents == -20000  # median(19900, 20100)
    assert stats[20].last_paid_on == date(2026, 5, 31)


def test_loan_payment_stats_ignores_non_loan_like() -> None:
    links = [
        _link(
            inflow_account_id=30,
            outflow_amount_cents=-5000,
            outflow_posted_on=date(2026, 5, 1),
            inflow_is_loan_like=False,
            inflow_account_type=AccountType.credit,
        )
    ]

    assert loan_payment_stats(links) == {}


async def test_classified_links_flags_loan_like(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan)
    financing_credit = await make_account(session, type=AccountType.credit, financing_mode=True)
    plain_credit = await make_account(session, type=AccountType.credit)

    async def _pair(target: Account, amount: int) -> None:
        out = await make_txn(session, checking, amount_cents=-amount, posted_on=date(2026, 7, 1))
        inn = await make_txn(session, target, amount_cents=amount, posted_on=date(2026, 7, 1))
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )

    await _pair(loan, 13500)
    await _pair(financing_credit, 20000)
    await _pair(plain_credit, 5000)
    await session.flush()

    links = await classified_links(session)
    by_inflow = {link.inflow_account_id: link for link in links}

    assert by_inflow[loan.id].inflow_is_loan_like is True
    assert by_inflow[loan.id].outflow_amount_cents == -13500
    assert by_inflow[financing_credit.id].inflow_is_loan_like is True
    assert by_inflow[financing_credit.id].outflow_amount_cents == -20000
    assert by_inflow[plain_credit.id].inflow_is_loan_like is False
    assert by_inflow[plain_credit.id].outflow_amount_cents == -5000
