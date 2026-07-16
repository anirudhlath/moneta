"""Detect credit accounts used as promo-financing vehicles (design 2026-07-16 §2).

Fingerprint: owed balance, periodic near-equal payment credits, and no significant
purchase debits since payments began. Deterministic — fires a one-time human review
question; the ReviewItem ledger (open or resolved) is the never-re-ask memory.
"""

import statistics

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, ReviewItem, ReviewKind, Transaction

_AMOUNT_TOLERANCE = 0.20  # near-equal payments: within ±20% of the credit median
_MINOR_FRACTION = 0.25  # debits under 25% of the median credit are fees, not purchases
_MIN_PAYMENTS = 2


async def detect_financing(session: AsyncSession) -> int:
    asked: set[int] = set()
    items = (
        await session.execute(
            select(ReviewItem).where(ReviewItem.kind == ReviewKind.financing_account)
        )
    ).scalars()
    for item in items:
        account_id = item.payload.get("account_id")
        if isinstance(account_id, int):
            asked.add(account_id)
    candidates = (
        (
            await session.execute(
                select(Account).where(
                    Account.type == AccountType.credit,
                    Account.financing_mode.is_(False),
                    Account.balance_cents < 0,
                )
            )
        )
        .scalars()
        .all()
    )
    opened = 0
    for acct in candidates:
        if acct.id in asked:
            continue
        txns = (
            (await session.execute(select(Transaction).where(Transaction.account_id == acct.id)))
            .scalars()
            .all()
        )
        credits = sorted((t for t in txns if t.amount_cents > 0), key=lambda t: t.posted_on)
        if len(credits) < _MIN_PAYMENTS:
            continue
        median_credit = statistics.median([t.amount_cents for t in credits])
        near_median = sum(
            1
            for t in credits
            if abs(t.amount_cents - median_credit) <= median_credit * _AMOUNT_TOLERANCE
        )
        if near_median < 2:
            continue
        payments_began = credits[0].posted_on
        purchasing = any(
            t.amount_cents < 0
            and abs(t.amount_cents) >= median_credit * _MINOR_FRACTION
            and t.posted_on >= payments_began
            for t in txns
        )
        if purchasing:
            continue
        session.add(
            ReviewItem(
                kind=ReviewKind.financing_account,
                question=(
                    f"{acct.name!r} looks like promo financing being paid down — "
                    "treat its payments as fixed costs?"
                ),
                payload={"account_id": acct.id},
            )
        )
        opened += 1
    await session.commit()
    return opened
