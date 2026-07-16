from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, ReviewItem, ReviewKind, ReviewStatus
from moneta.pipelines.financing import detect_financing
from moneta.pipelines.review import apply_resolution
from tests.factories import make_account, make_txn


async def _open_financing_items(session: AsyncSession) -> list[ReviewItem]:
    return list(
        (
            await session.execute(
                select(ReviewItem).where(
                    ReviewItem.kind == ReviewKind.financing_account,
                    ReviewItem.status == ReviewStatus.open,
                )
            )
        ).scalars()
    )


async def test_fires_on_payments_only(session: AsyncSession) -> None:
    acct = await make_account(
        session, type=AccountType.credit, balance_cents=-161105, balance_date=date(2026, 7, 1)
    )
    for d in (date(2026, 4, 5), date(2026, 5, 5), date(2026, 6, 5)):
        await make_txn(session, acct, amount_cents=6444, posted_on=d, description="PAYMENT")
    opened = await detect_financing(session)
    assert opened == 1
    items = await _open_financing_items(session)
    assert len(items) == 1
    assert items[0].payload == {"account_id": acct.id}


async def test_payoff_outlier_still_fires(session: AsyncSession) -> None:
    """CareCredit: two normal payments plus one big payoff — outlier tolerated."""
    acct = await make_account(
        session, type=AccountType.credit, balance_cents=-50000, balance_date=date(2026, 7, 1)
    )
    for amount, d in (
        (9900, date(2026, 4, 5)),
        (9600, date(2026, 5, 5)),
        (284794, date(2026, 6, 5)),
    ):
        await make_txn(session, acct, amount_cents=amount, posted_on=d, description="PAYMENT")
    # paper fee: -299 is < 25% of the 9900 median, so it isn't a purchase
    await make_txn(
        session, acct, amount_cents=-299, posted_on=date(2026, 4, 10), description="PAPER FEE"
    )
    opened = await detect_financing(session)
    assert opened == 1


async def test_purchase_before_payments_fires(session: AsyncSession) -> None:
    """Modani: the furniture purchase predates the first payment — still financing."""
    acct = await make_account(
        session, type=AccountType.credit, balance_cents=-349324, balance_date=date(2026, 7, 1)
    )
    await make_txn(
        session, acct, amount_cents=-349324, posted_on=date(2026, 4, 1), description="MODANI"
    )
    await make_txn(
        session, acct, amount_cents=300000, posted_on=date(2026, 4, 8), description="PAYMENT"
    )
    await make_txn(
        session, acct, amount_cents=300000, posted_on=date(2026, 5, 8), description="PAYMENT"
    )
    opened = await detect_financing(session)
    assert opened == 1


async def test_daily_use_never_fires(session: AsyncSession) -> None:
    """OnePay: payments are similar in size to ordinary daily spend — not financing."""
    acct = await make_account(
        session, type=AccountType.credit, balance_cents=-40000, balance_date=date(2026, 7, 1)
    )
    for amount, d in (
        (6000, date(2026, 4, 1)),
        (6000, date(2026, 5, 1)),
        (6000, date(2026, 6, 1)),
    ):
        await make_txn(session, acct, amount_cents=amount, posted_on=d, description="PAYMENT")
    debit_amounts = [-2000, -3000, -4000, -5000, -6000, -7000, -8000, -2500, -3500, -4500]
    for i, amount in enumerate(debit_amounts):
        await make_txn(
            session,
            acct,
            amount_cents=amount,
            posted_on=date(2026, 4, 2 + i),
            description=f"PURCHASE {i}",
        )
    opened = await detect_financing(session)
    assert opened == 0
    assert await _open_financing_items(session) == []


async def test_positive_balance_never_fires(session: AsyncSession) -> None:
    acct = await make_account(
        session, type=AccountType.credit, balance_cents=500, balance_date=date(2026, 7, 1)
    )
    for d in (date(2026, 4, 5), date(2026, 5, 5), date(2026, 6, 5)):
        await make_txn(session, acct, amount_cents=6444, posted_on=d, description="PAYMENT")
    opened = await detect_financing(session)
    assert opened == 0
    assert await _open_financing_items(session) == []


async def test_one_payment_never_fires(session: AsyncSession) -> None:
    acct = await make_account(
        session, type=AccountType.credit, balance_cents=-16000, balance_date=date(2026, 7, 1)
    )
    await make_txn(session, acct, amount_cents=6444, posted_on=date(2026, 6, 5), description="PMT")
    opened = await detect_financing(session)
    assert opened == 0
    assert await _open_financing_items(session) == []


async def test_asked_once_never_reasked(session: AsyncSession) -> None:
    acct = await make_account(
        session, type=AccountType.credit, balance_cents=-161105, balance_date=date(2026, 7, 1)
    )
    for d in (date(2026, 4, 5), date(2026, 5, 5), date(2026, 6, 5)):
        await make_txn(session, acct, amount_cents=6444, posted_on=d, description="PAYMENT")
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="already asked",
            payload={"account_id": acct.id},
            status=ReviewStatus.resolved,
            resolution={"financing": False, "resolved_by": "manual"},
        )
    )
    await session.flush()
    opened = await detect_financing(session)
    assert opened == 0
    assert await _open_financing_items(session) == []


async def test_resolution_true_sets_financing_mode(session: AsyncSession) -> None:
    acct_true = await make_account(session, type=AccountType.credit, balance_cents=-16000)
    item_true = ReviewItem(
        kind=ReviewKind.financing_account,
        question="q",
        payload={"account_id": acct_true.id},
    )
    session.add(item_true)
    await session.flush()
    await apply_resolution(session, item_true, {"financing": True})
    assert acct_true.financing_mode is True
    assert item_true.status == ReviewStatus.resolved

    acct_false = await make_account(session, type=AccountType.credit, balance_cents=-16000)
    item_false = ReviewItem(
        kind=ReviewKind.financing_account,
        question="q",
        payload={"account_id": acct_false.id},
    )
    session.add(item_false)
    await session.flush()
    await apply_resolution(session, item_false, {"financing": False})
    assert acct_false.financing_mode is False
    assert item_false.status == ReviewStatus.resolved


async def test_apply_resolution_missing_account_is_noop(session: AsyncSession) -> None:
    """A stale payload (account deleted) must not blow up resolution."""
    item = ReviewItem(
        kind=ReviewKind.financing_account,
        question="q",
        payload={"account_id": 999999},
    )
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"financing": True})
    assert item.status == ReviewStatus.resolved
    assert (await session.execute(select(Account))).scalar_one_or_none() is None
