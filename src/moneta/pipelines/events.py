from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    EventKind,
    RecurringSeries,
    SeriesEvent,
    SeriesStatus,
    Transaction,
)
from moneta.pipelines.recurring import GRACE_DAYS, advance_expected_on, missed_event

_PRICE_CHANGE_THRESHOLD = 0.05


async def emit_series_events(session: AsyncSession, today: date) -> int:
    emitted = 0
    series_list = (
        (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.active)
            )
        )
        .scalars()
        .all()
    )
    for s in series_list:
        grace = timedelta(days=GRACE_DAYS[s.cadence])

        while today > s.next_expected_on + grace:
            window_hit = (
                await session.execute(
                    select(Transaction.id)
                    .where(
                        Transaction.series_id == s.id,
                        Transaction.posted_on >= s.next_expected_on - grace,
                        Transaction.posted_on <= s.next_expected_on + grace,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if window_hit is None:
                session.add(missed_event(s.id, s.next_expected_on))
                emitted += 1
            s.next_expected_on = advance_expected_on(s.next_expected_on, s.cadence)

        latest_two = (
            (
                await session.execute(
                    select(Transaction)
                    .where(Transaction.series_id == s.id)
                    .order_by(Transaction.posted_on.desc(), Transaction.id.desc())
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        # one sample is an outlier until a second occurrence confirms the new price
        if len(latest_two) == 2 and s.expected_cents != 0:
            newest, prior = latest_two
            drift = abs(newest.amount_cents - s.expected_cents) / abs(s.expected_cents)
            settled = (
                abs(newest.amount_cents - prior.amount_cents)
                <= abs(newest.amount_cents) * _PRICE_CHANGE_THRESHOLD
            )
            if drift > _PRICE_CHANGE_THRESHOLD and settled:
                session.add(
                    SeriesEvent(
                        series_id=s.id,
                        kind=EventKind.price_increase,
                        occurred_on=newest.posted_on,
                        details={"old_cents": s.expected_cents, "new_cents": newest.amount_cents},
                    )
                )
                s.expected_cents = newest.amount_cents
                emitted += 1
    await session.commit()
    return emitted
