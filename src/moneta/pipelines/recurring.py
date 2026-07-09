import statistics
from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier
from moneta.models import (
    AccountType,
    Cadence,
    Direction,
    EventKind,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
    Transaction,
)
from moneta.queries import classified_links

CADENCE_DAYS: dict[Cadence, int] = {
    Cadence.weekly: 7,
    Cadence.biweekly: 14,
    Cadence.monthly: 30,
    Cadence.annual: 365,
}
_TOLERANCE: dict[Cadence, int] = {
    Cadence.weekly: 2,
    Cadence.biweekly: 3,
    Cadence.monthly: 6,
    Cadence.annual: 20,
}
_MIN_OCCURRENCES = 3
_AMOUNT_TOLERANCE = 0.20
_STALE_PERIODS = 3
_PER_MONTH: dict[Cadence, float] = {
    Cadence.weekly: 52 / 12,
    Cadence.biweekly: 26 / 12,
    Cadence.monthly: 1.0,
    Cadence.annual: 1 / 12,
}

_LLM_PROMPT = """Is this group of bank transactions one recurring bill/subscription?
Merchant: {merchant!r}; amounts (cents) and dates: {rows}
Respond with JSON: {{"is_recurring": true/false}}"""


class RecurringStats(BaseModel):
    new_series: int = 0
    updated: int = 0
    review: int = 0


def monthly_cents(series: RecurringSeries) -> int:
    return round(series.expected_cents * _PER_MONTH[series.cadence])


def _match_cadence(dates: list[date]) -> tuple[Cadence, int] | None:
    """Best cadence and the start index of the newest run matching it.

    Deep history contains breaks (pauses, resubscriptions, card reissues); judging
    cadence on the maximal recent run keeps ancient gaps from poisoning a
    currently-clean series.
    """
    for cadence, days in CADENCE_DAYS.items():
        tol = _TOLERANCE[cadence]
        start = len(dates) - 1
        while start > 0 and abs((dates[start] - dates[start - 1]).days - days) <= tol * 2:
            start -= 1
        run = dates[start:]
        if len(run) < _MIN_OCCURRENCES:
            continue
        gaps = [(b - a).days for a, b in zip(run, run[1:], strict=False)]
        if abs(statistics.median(gaps) - days) <= tol:
            return cadence, start
    return None


async def _excluded_txn_ids(session: AsyncSession) -> set[int]:
    """Transfer-linked txns are excluded UNLESS the link pays into a loan account."""
    excluded: set[int] = set()
    for link in await classified_links(session):
        excluded.add(link.inflow_id)  # inflow side is never a spend/income signal
        if link.inflow_account_type != AccountType.loan:
            excluded.add(link.outflow_id)
    return excluded


async def detect_recurring(
    session: AsyncSession, llm: Classifier | None, today: date
) -> RecurringStats:
    stats = RecurringStats()
    excluded = await _excluded_txn_ids(session)
    reviewed: set[str] = set()
    force: dict[str, bool] = {}
    for item in (
        await session.execute(
            select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
        )
    ).scalars():
        merchant_key = item.payload.get("merchant")
        if not isinstance(merchant_key, str):
            continue
        if item.status == ReviewStatus.open:
            reviewed.add(merchant_key)
        elif isinstance(item.resolution, dict) and isinstance(
            item.resolution.get("is_recurring"), bool
        ):
            force[merchant_key] = item.resolution["is_recurring"]
    existing = {
        (s.merchant, s.direction): s
        for s in (await session.execute(select(RecurringSeries))).scalars()
    }
    txns = (
        (
            await session.execute(
                select(Transaction)
                .where(Transaction.merchant.is_not(None))
                .order_by(Transaction.posted_on, Transaction.id)
            )
        )
        .scalars()
        .all()
    )
    groups: dict[tuple[str, Direction], list[Transaction]] = {}
    for t in txns:
        if t.id in excluded or t.merchant is None:
            continue
        direction = Direction.outflow if t.amount_cents < 0 else Direction.inflow
        groups.setdefault((t.merchant, direction), []).append(t)

    for (merchant, direction), group in groups.items():
        if len(group) < _MIN_OCCURRENCES:
            continue
        dates = sorted({t.posted_on for t in group})  # dedup: double-posts aren't a 0-day gap
        match = _match_cadence(dates)
        if match is None:
            continue
        cadence, start = match
        run = [t for t in group if t.posted_on >= dates[start]]
        amounts = [abs(t.amount_cents) for t in run]
        med = statistics.median(amounts)
        stable = all(abs(a - med) <= med * _AMOUNT_TOLERANCE for a in amounts)
        expected = -round(med) if direction == Direction.outflow else round(med)
        if not stable:
            forced = force.get(merchant)
            if forced is False:
                continue  # user-resolved as not recurring — suppress silently, forever
            if forced is not True:  # not resolved (or resolved True skips straight to series)
                answer = (
                    await llm.classify_json(
                        _LLM_PROMPT.format(
                            merchant=merchant,
                            rows=[(t.amount_cents, t.posted_on.isoformat()) for t in group],
                        )
                    )
                    if llm
                    else None
                )
                if not (answer and answer.get("is_recurring")):
                    if merchant not in reviewed:
                        session.add(
                            ReviewItem(
                                kind=ReviewKind.recurring_cluster,
                                question=f"Is {merchant!r} a recurring bill?",
                                payload={"merchant": merchant, "direction": direction},
                            )
                        )
                        stats.review += 1
                    continue
        next_on = dates[-1] + timedelta(days=CADENCE_DAYS[cadence])
        stale = (today - dates[-1]).days > _STALE_PERIODS * CADENCE_DAYS[cadence]
        series = existing.get((merchant, direction))
        if series is None:
            series = RecurringSeries(
                merchant=merchant,
                direction=direction,
                cadence=cadence,
                expected_cents=expected,
                next_expected_on=next_on,
                status=SeriesStatus.ended if stale else SeriesStatus.active,
            )
            session.add(series)
            await session.flush()
            session.add(
                SeriesEvent(
                    series_id=series.id,
                    kind=EventKind.new_series,
                    occurred_on=dates[-1],
                    details={"merchant": merchant},
                )
            )
            stats.new_series += 1
        else:
            advanced_on = max(series.next_expected_on, next_on)
            if stale:
                status = SeriesStatus.ended
            elif series.status == SeriesStatus.ended and group[-1].series_id is None:
                # the newest occurrence is untagged ⇒ genuinely new since the last run: an
                # ended series (auto- or manually) that charges again at cadence revives
                status = SeriesStatus.active
            else:
                status = SeriesStatus(series.status)
            changed = (
                series.next_expected_on != advanced_on
                or series.cadence != cadence
                or series.status != status
            )
            series.cadence = cadence
            series.next_expected_on = advanced_on
            series.status = status
            if changed:
                stats.updated += 1
        for t in group:
            t.series_id = series.id

    # Groups that no longer match a cadence (trailing noise, shrunk by exclusions) never
    # reach the stale check above — sweep every still-active series by its own txns.
    for series in existing.values():
        if series.status != SeriesStatus.active:
            continue
        newest = (
            await session.execute(
                select(func.max(Transaction.posted_on)).where(Transaction.series_id == series.id)
            )
        ).scalar_one_or_none()
        period = CADENCE_DAYS[series.cadence]
        last_seen = newest or series.next_expected_on - timedelta(days=period)
        if (today - last_seen).days > _STALE_PERIODS * period:
            series.status = SeriesStatus.ended
            stats.updated += 1
    await session.commit()
    return stats
