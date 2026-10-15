from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.logical import load_logical_data
from moneta.models import Account, AccountType, Cadence, Transaction, TransferLink
from moneta.queries import (
    ClassifiedLink,
    StaleAccount,
    _posting_rate_phrase,
    classified_links,
    feed_is_stale,
    loan_payment_stats,
    stale_accounts,
)
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


_TODAY = date(2026, 7, 9)


async def _post_on(
    session: AsyncSession, account: Account, days_ago: Iterable[int]
) -> list[Transaction]:
    """One transaction per listed day before _TODAY, in the order given."""
    return [
        await make_txn(session, account, posted_on=_TODAY - timedelta(days=d)) for d in days_ago
    ]


def test_feed_is_stale_boundaries() -> None:
    # 25 posting days a year over 73 quiet days is exactly 5 expected posting days
    assert feed_is_stale(posting_days=25, days_since=73)
    assert not feed_is_stale(posting_days=24, days_since=73)
    # even a daily feed gets a full week before it is called quiet
    assert not feed_is_stale(posting_days=365, days_since=6)
    assert feed_is_stale(posting_days=365, days_since=7)
    assert not feed_is_stale(posting_days=0, days_since=365)


def test_posting_rate_phrase() -> None:
    assert _posting_rate_phrase(187) == "~4 days a week"
    assert _posting_rate_phrase(52) == "~1 day a week"
    assert _posting_rate_phrase(24) == "~2 days a month"
    assert _posting_rate_phrase(5) == "~1 day a month"


async def test_daily_card_quiet_for_two_weeks_is_stale(session: AsyncSession) -> None:
    card = await make_account(session, name="Acme Card", mask="1234", type=AccountType.credit)
    await _post_on(session, card, range(14, 201))  # daily, then silent for 14 days
    stale = await stale_accounts(session, await load_logical_data(session), _TODAY)
    assert stale == [
        StaleAccount(
            account_id=card.id,
            name="Acme Card",
            mask="1234",
            newest_posted_on=date(2026, 6, 25),
            posting_days=187,
            note=(
                "Acme Card ··1234 has no transactions since 2026-06-25 "
                "(usually posts ~4 days a week) — recent spending on it may be missing."
            ),
        )
    ]


async def test_monthly_savings_account_is_not_stale(session: AsyncSession) -> None:
    savings = await make_account(session, name="Acme Savings", type=AccountType.savings)
    await _post_on(session, savings, range(40, 400, 30))  # monthly; newest 40 days ago
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_account_fresh_via_either_provider_is_not_stale(session: AsyncSession) -> None:
    root = await make_account(session, source="simplefin", name="Acme Card", mask="1234")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    await _post_on(session, root, range(20, 201))  # this provider stalled 20 days ago
    await _post_on(session, alias, range(1, 20))  # the other one kept posting
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_suppressed_duplicate_still_counts_as_the_newest_observation(
    session: AsyncSession,
) -> None:
    """Overlap kept the older-dated observation of the newest purchase; the other
    provider's row, four days fresher, still proves the feed is alive."""
    root = await make_account(session, source="simplefin", name="Acme Card", mask="1234")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    survivors = await _post_on(session, root, range(9, 221))  # stale on survivors alone
    await make_txn(
        session,
        alias,
        posted_on=_TODAY - timedelta(days=5),
        duplicate_of_id=survivors[0].id,
    )
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_cross_provider_duplicates_count_one_posting_day(session: AsyncSession) -> None:
    """One purchase seen by both providers a day apart is one posting day: counting
    both observations would double this weekly account's rate and flag it."""
    root = await make_account(session, source="simplefin", name="Acme Card", mask="1234")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    for week in range(50):  # weekly, newest 20 days ago
        survivor = await make_txn(session, root, posted_on=_TODAY - timedelta(days=20 + 7 * week))
        await make_txn(
            session,
            alias,
            posted_on=_TODAY - timedelta(days=21 + 7 * week),
            duplicate_of_id=survivor.id,
        )
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_several_txns_on_one_day_are_one_posting_day(session: AsyncSession) -> None:
    """The rule counts distinct posting days, not transactions: 30 busy days of 3
    transactions each is 30 posting days (900 < 5 * 365), not 90 (2700 >= 1825)."""
    card = await make_account(session, name="Acme Card", mask="1234")
    busy_days = range(30, 330, 10)  # 30 distinct days, newest 30 days ago
    await _post_on(session, card, [d for d in busy_days for _ in range(3)])
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_txns_only_older_than_the_window_never_flag(session: AsyncSession) -> None:
    old = await make_account(session, name="Elmton Card")
    await _post_on(session, old, range(366, 500))  # daily, but over a year ago
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def _quiet_account_with_one_more_day(
    session: AsyncSession, extra_days_ago: int
) -> list[StaleAccount]:
    """24 posting days (newest 73 back) plus one more `extra_days_ago`; with that day
    counted the account sits exactly on the 25 x 73 = 1825 boundary and is flagged."""
    card = await make_account(session, name="Acme Card", mask="1234")
    await _post_on(session, card, [*range(73, 97), extra_days_ago])
    return await stale_accounts(session, await load_logical_data(session), _TODAY)


async def test_a_posting_day_exactly_a_year_back_is_outside_the_window(
    session: AsyncSession,
) -> None:
    """The window is (today - 365, today]: 365 days ago is out, so 24 posting days."""
    assert await _quiet_account_with_one_more_day(session, 365) == []


async def test_a_posting_day_just_inside_the_window_counts(session: AsyncSession) -> None:
    """364 days ago is the oldest counted day: 25 posting days, exactly on the boundary."""
    stale = await _quiet_account_with_one_more_day(session, 364)
    assert [(s.name, s.posting_days) for s in stale] == [("Acme Card", 25)]


async def test_a_future_dated_txn_proves_the_feed_is_alive(session: AsyncSession) -> None:
    """A pending post dated tomorrow is the newest observation, so it is not a quiet feed."""
    card = await make_account(session, name="Acme Card", mask="1234")
    await _post_on(session, card, range(73, 98))  # 25 posting days, newest 73 back: flagged
    logical = await load_logical_data(session)
    assert [s.account_id for s in await stale_accounts(session, logical, _TODAY)] == [card.id]
    await make_txn(session, card, posted_on=_TODAY + timedelta(days=1))
    logical = await load_logical_data(session)
    assert await stale_accounts(session, logical, _TODAY) == []


async def test_single_txn_account_never_flags(session: AsyncSession) -> None:
    for days_ago in (7, 30, 200, 364):
        account = await make_account(session)
        await make_txn(session, account, posted_on=_TODAY - timedelta(days=days_ago))
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_brand_new_and_empty_accounts_are_not_stale(session: AsyncSession) -> None:
    new = await make_account(session)
    await _post_on(session, new, range(0, 4))  # first sync brought only this week
    await make_account(session, type=AccountType.brokerage)  # holdings only, no txns
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []
