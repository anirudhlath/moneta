from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Cadence,
    EventKind,
    RecurringSeries,
    SeriesEvent,
    SeriesStatus,
    Transaction,
)
from moneta.pipelines.recurring import CADENCE_DAYS

_GRACE: dict[Cadence, int] = {
    Cadence.weekly: 3,
    Cadence.biweekly: 4,
    Cadence.monthly: 7,
    Cadence.annual: 30,
}
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
        grace = timedelta(days=_GRACE[s.cadence])
        period = timedelta(days=CADENCE_DAYS[s.cadence])

        if today > s.next_expected_on + grace:
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
                session.add(
                    SeriesEvent(
                        series_id=s.id,
                        kind=EventKind.missed,
                        occurred_on=s.next_expected_on,
                        details={"expected_on": s.next_expected_on.isoformat()},
                    )
                )
                emitted += 1
            s.next_expected_on = s.next_expected_on + period

        latest = (
            await session.execute(
                select(Transaction)
                .where(Transaction.series_id == s.id)
                .order_by(Transaction.posted_on.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None and s.expected_cents != 0:
            drift = abs(latest.amount_cents - s.expected_cents) / abs(s.expected_cents)
            if drift > _PRICE_CHANGE_THRESHOLD:
                session.add(
                    SeriesEvent(
                        series_id=s.id,
                        kind=EventKind.price_increase,
                        occurred_on=latest.posted_on,
                        details={"old_cents": s.expected_cents, "new_cents": latest.amount_cents},
                    )
                )
                s.expected_cents = latest.amount_cents
                emitted += 1
    await session.commit()
    return emitted
