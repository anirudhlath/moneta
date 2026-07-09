from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier, confident_yes
from moneta.models import (
    Cadence,
    EventKind,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
    Transaction,
    dollars,
)
from moneta.pipelines.recurring import CADENCE_DAYS

_GRACE: dict[Cadence, int] = {
    Cadence.weekly: 3,
    Cadence.biweekly: 4,
    Cadence.monthly: 7,
    Cadence.annual: 30,
}
_PRICE_CHANGE_THRESHOLD = 0.05

_PRICE_PROMPT = """You are double-checking a detected price change on a recurring bill.
Series: {merchant!r}, expected ${old} {cadence}; latest charge ${new} on {posted_on}.
Is this a genuine new price for the same bill (vs. a one-off or unrelated charge)?
Respond with JSON: {{"is_price_change": true/false, "confident": true/false}}
Set confident=true ONLY if you are sure either way."""


async def _confirms_price_change(
    llm: Classifier, series: RecurringSeries, latest: Transaction
) -> bool:
    answer = await llm.classify_json(
        _PRICE_PROMPT.format(
            merchant=series.merchant,
            old=dollars(series.expected_cents),
            cadence=series.cadence,
            new=dollars(latest.amount_cents),
            posted_on=latest.posted_on.isoformat(),
        )
    )
    return confident_yes(answer, "is_price_change")


def apply_price_change(
    session: AsyncSession, series: RecurringSeries, new_cents: int, occurred_on: date
) -> None:
    """Record a confirmed price change: emit the event, then move expected_cents."""
    session.add(
        SeriesEvent(
            series_id=series.id,
            kind=EventKind.price_increase,
            occurred_on=occurred_on,
            details={"old_cents": series.expected_cents, "new_cents": new_cents},
        )
    )
    series.expected_cents = new_cents


async def emit_series_events(session: AsyncSession, llm: Classifier | None, today: date) -> int:
    emitted = 0
    # price-change questions already in flight (open) or answered "no" (denied)
    open_series: set[int] = set()
    denied: set[tuple[int, int]] = set()
    for item in (
        await session.execute(select(ReviewItem).where(ReviewItem.kind == ReviewKind.price_change))
    ).scalars():
        sid = item.payload.get("series_id")
        if not isinstance(sid, int):
            continue
        if item.status == ReviewStatus.open:
            open_series.add(sid)
        elif isinstance(item.resolution, dict) and item.resolution.get("is_price_change") is False:
            new_cents = item.payload.get("new_cents")
            if isinstance(new_cents, int):
                denied.add((sid, new_cents))
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
        if latest is None or s.expected_cents == 0:
            continue
        drift = abs(latest.amount_cents - s.expected_cents) / abs(s.expected_cents)
        if (
            drift <= _PRICE_CHANGE_THRESHOLD
            or s.id in open_series
            or (s.id, latest.amount_cents) in denied
        ):
            continue
        if llm is None or await _confirms_price_change(llm, s, latest):
            apply_price_change(session, s, latest.amount_cents, latest.posted_on)
            emitted += 1
        else:
            session.add(
                ReviewItem(
                    kind=ReviewKind.price_change,
                    question=(
                        f"Did {s.merchant!r} change price from "
                        f"${dollars(s.expected_cents)} to ${dollars(latest.amount_cents)}?"
                    ),
                    payload={
                        "series_id": s.id,
                        "merchant": s.merchant,
                        "old_cents": s.expected_cents,
                        "new_cents": latest.amount_cents,
                        "occurred_on": latest.posted_on.isoformat(),
                        "llm_flagged": True,
                    },
                )
            )
    await session.commit()
    return emitted
