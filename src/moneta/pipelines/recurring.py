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
    series_key,
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
# charges below this fraction of the group's median are adjustments, not occurrences
_MINOR_FRACTION = 0.25
# cadence-miss groups only warrant a review question when timing is bill-like,
# not habitual spending (coffee, rideshare) with sub-weekly bursts
_MIN_REVIEW_GAP_DAYS = 10
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


def _stale(last_seen: date, cadence: Cadence, today: date) -> bool:
    """A series is stale once its newest occurrence is over 3 cadence periods old."""
    return (today - last_seen).days > _STALE_PERIODS * CADENCE_DAYS[cadence]


def _match_cadence(dates: list[date]) -> tuple[Cadence, date] | None:
    """Best cadence and the start date of the newest run matching it.

    Deep history contains breaks (pauses, resubscriptions, card reissues); judging
    cadence on the maximal recent run keeps ancient gaps from poisoning a
    currently-clean series.
    """
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    for cadence, days in CADENCE_DAYS.items():
        tol = _TOLERANCE[cadence]
        start = len(dates) - 1
        while start > 0 and abs(gaps[start - 1] - days) <= tol * 2:
            start -= 1
        if len(dates) - start < _MIN_OCCURRENCES:
            continue
        if abs(statistics.median(gaps[start:]) - days) <= tol:
            return cadence, dates[start]
    return None


def _median_gap(dates: list[date]) -> float:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    return float(statistics.median(gaps))


def _closest_cadence(dates: list[date]) -> Cadence:
    med = _median_gap(dates)
    return min(CADENCE_DAYS, key=lambda c: abs(CADENCE_DAYS[c] - med))


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
    reviewed: set[tuple[str, str]] = set()
    force: dict[tuple[str, str], bool] = {}
    for item in (
        await session.execute(
            select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
        )
    ).scalars():
        # direction-scoped: an answer about outflows must not force inflows
        key = series_key(item.payload.get("merchant"), item.payload.get("direction"))
        if key is None:
            continue
        if item.status == ReviewStatus.open:
            reviewed.add(key)
        elif isinstance(item.resolution, dict) and isinstance(
            item.resolution.get("is_recurring"), bool
        ):
            force[key] = item.resolution["is_recurring"]
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
        scale = statistics.median([abs(t.amount_cents) for t in group])
        significant = [t for t in group if abs(t.amount_cents) >= scale * _MINOR_FRACTION]
        if len(significant) < _MIN_OCCURRENCES:
            continue
        dates = sorted({t.posted_on for t in significant})  # dedup: double-posts aren't 0-day gaps
        match = _match_cadence(dates)
        if match is None:
            cadence, run = None, significant
        else:
            # stats come from the newest cadence-run so ancient price epochs don't skew them
            cadence, run_start = match
            run = [t for t in significant if t.posted_on >= run_start]
        amounts = [abs(t.amount_cents) for t in run]
        med = statistics.median(amounts)
        stable = all(abs(a - med) <= med * _AMOUNT_TOLERANCE for a in amounts)
        expected = -round(med) if direction == Direction.outflow else round(med)
        forced = force.get((merchant, str(direction)))
        if forced is False:
            continue  # user-resolved as not recurring — suppress silently, forever

        def _open_review(merchant: str = merchant, direction: Direction = direction) -> None:
            if (merchant, str(direction)) not in reviewed:
                session.add(
                    ReviewItem(
                        kind=ReviewKind.recurring_cluster,
                        question=f"Is {merchant!r} a recurring bill?",
                        payload={"merchant": merchant, "direction": direction},
                    )
                )
                stats.review += 1

        if cadence is None:
            if forced is not True:  # irregular timing needs a human, not the LLM
                # only ask when it plausibly IS a bill: steady amounts at bill-like intervals
                if stable and _median_gap(dates) >= _MIN_REVIEW_GAP_DAYS:
                    _open_review()
                continue
            cadence = _closest_cadence(dates)
        elif not stable and forced is not True:
            answer = (
                await llm.classify_json(
                    _LLM_PROMPT.format(
                        merchant=merchant,
                        rows=[(t.amount_cents, t.posted_on.isoformat()) for t in run],
                    )
                )
                if llm
                else None
            )
            if not (answer and answer.get("is_recurring")):
                _open_review()
                continue
        next_on = dates[-1] + timedelta(days=CADENCE_DAYS[cadence])
        stale = _stale(dates[-1], cadence, today)
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
            elif series.status == SeriesStatus.ended and significant[-1].series_id is None:
                # the newest occurrence is untagged ⇒ genuinely new since the last run: an
                # ended series (auto- or manually) that charges again at cadence revives
                # (minor adjustments are never tagged, so they can't trigger this)
                status = SeriesStatus.active
            else:
                status = series.status
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
        for t in significant:
            t.series_id = series.id

    # Groups that no longer match a cadence (trailing noise, shrunk by exclusions) never
    # reach the stale check above — sweep every still-active series by its own txns.
    newest_rows = (
        await session.execute(
            select(Transaction.series_id, func.max(Transaction.posted_on))
            .where(Transaction.series_id.is_not(None))
            .group_by(Transaction.series_id)
        )
    ).all()
    newest_by_series: dict[int | None, date] = {sid: newest for sid, newest in newest_rows}
    for series in existing.values():
        if series.status != SeriesStatus.active:
            continue
        # detect_recurring tags every occurrence it matches, so an active series always
        # has tagged txns; without any there is no evidence to judge — leave it alone.
        last_seen = newest_by_series.get(series.id)
        if last_seen is not None and _stale(last_seen, series.cadence, today):
            series.status = SeriesStatus.ended
            stats.updated += 1
    await session.commit()
    return stats
