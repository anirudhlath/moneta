from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, Cadence, ReviewItem, ReviewKind, TransferLink
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
    assert stats[20].expected_cents == -20100  # the newest payment, within 1.5x of the prior
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


def _monthly_links(account_id: int, amounts: list[int]) -> list[ClassifiedLink]:
    """One loan-like payment on the 1st of each month from 2026-04, oldest first."""
    return [
        _link(
            outflow_id=100 + i,
            inflow_id=200 + i,
            inflow_account_id=account_id,
            outflow_amount_cents=amount,
            outflow_posted_on=date(2026, 4 + i, 1),
            inflow_is_loan_like=True,
        )
        for i, amount in enumerate(amounts)
    ]


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        ([-12000, -11000, -10000, -7000], -7000),  # winding down: the newest is the obligation
        ([-5000, -5000, -5000, -20000], -5000),  # a one-off extra doesn't set it
        ([-5000], -5000),  # one payment is its own obligation
        ([-5000, -9000], -5000),  # 9000 > 1.5 x 5000: an extra
        ([-10000, -10000, -10000, -15000], -15000),  # exactly 1.5x is not "more than"
        ([-10000, -10000, -10000, -15001], -10000),
        # only the 3 payments before the newest are the yardstick, not older history
        ([-20000, -20000, -20000, -8000, -8000, -8000, -13000], -8000),
        # the window is exactly 3: a window of 2 or 4 would read a median of 11000 and keep 13000
        ([-30000, -8000, -8000, -14000, -13000], -8000),
        # a small extra doesn't set it either: below half the median is a one-off, too
        ([-30000, -30000, -30000, -5000], -30000),
        ([-30000, -30000, -30000, -15000], -15000),  # exactly half is not "less than half"
        ([-30000, -30000, -30000, -14999], -30000),
    ],
)
def test_loan_payment_stats_expected_is_newest_unless_one_off(
    amounts: list[int], expected: int
) -> None:
    stats = loan_payment_stats(_monthly_links(40, amounts))

    assert stats[40].expected_cents == expected
    assert stats[40].cadence == Cadence.monthly


def test_loan_payment_stats_small_off_day_extra_does_not_set_the_payment() -> None:
    """$300.00 on the 5th for four months, then a $50.00 extra on the 20th: the extra is
    below half the median, so the obligation stays $300.00."""
    payments = [(date(2026, month, 5), -30000) for month in (3, 4, 5, 6)]
    payments.append((date(2026, 6, 20), -5000))
    links = [
        _link(
            outflow_id=100 + i,
            inflow_id=200 + i,
            inflow_account_id=60,
            outflow_amount_cents=amount,
            outflow_posted_on=posted_on,
            inflow_is_loan_like=True,
        )
        for i, (posted_on, amount) in enumerate(payments)
    ]

    stats = loan_payment_stats(links)

    assert stats[60].expected_cents == -30000


def test_loan_payment_stats_sums_payments_on_one_day() -> None:
    """A payment split in two on one day is one payment of the sum, not two halves."""
    links = _monthly_links(50, [-10000, -10000, -5000]) + [
        _link(
            outflow_id=300,
            inflow_id=301,
            inflow_account_id=50,
            outflow_amount_cents=-5000,
            outflow_posted_on=date(2026, 6, 1),
            inflow_is_loan_like=True,
        )
    ]

    stats = loan_payment_stats(links)

    assert stats[50].expected_cents == -10000
    assert stats[50].cadence == Cadence.monthly
    assert stats[50].last_paid_on == date(2026, 6, 1)


async def test_classified_links_open_financing_question_is_loan_like(
    session: AsyncSession,
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(session, type=AccountType.credit, balance_cents=-90000)
    out = await make_txn(session, checking, amount_cents=-10000, posted_on=date(2026, 7, 1))
    inn = await make_txn(session, card, amount_cents=10000, posted_on=date(2026, 7, 1))
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="q",
            payload={"account_id": card.id},
        )
    )
    await session.flush()

    [link] = await classified_links(session)

    assert link.inflow_is_loan_like is True
