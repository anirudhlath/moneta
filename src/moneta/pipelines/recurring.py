import statistics
from datetime import date

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import CADENCE_DAYS as CADENCE_DAYS
from moneta.cadence import GRACE_DAYS as GRACE_DAYS
from moneta.cadence import advance_expected_on as advance_expected_on
from moneta.cadence import match_cadence, monthlyize
from moneta.llm import Classifier
from moneta.models import (
    Account,
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
from moneta.queries import classified_links, primary_currency

_MIN_OCCURRENCES = 3
_AMOUNT_TOLERANCE = 0.20
_STALE_PERIODS = 3
# charges below this fraction of the group's median are adjustments, not occurrences
_MINOR_FRACTION = 0.25
# cadence-miss groups only warrant a review question when timing is bill-like,
# not habitual spending (coffee, rideshare) with sub-weekly bursts
_MIN_REVIEW_GAP_DAYS = 10


def missed_event(series_id: int, window: date) -> SeriesEvent:
    """The one shape of a missed-payment event — emitted here and by events.py."""
    return SeriesEvent(
        series_id=series_id,
        kind=EventKind.missed,
        occurred_on=window,
        details={"expected_on": window.isoformat()},
    )


def reactivate_series(series: RecurringSeries, today: date) -> None:
    """Forward-only bump: reactivating must not resurrect ancient missed windows."""
    series.next_expected_on = max(series.next_expected_on, today)
    series.status = SeriesStatus.active


_LLM_PROMPT = """Classify this group of bank transactions from one merchant.
- "bill": a fixed obligation — subscription, rent, insurance, loan or membership payment; \
roughly stable amount; there are consequences if unpaid.
- "habit": recurring discretionary spending — restaurants, coffee, bars, groceries, \
rideshare; variable amounts; a fresh choice each time.
- "not_recurring": neither — coincidental repetition.
Merchant: {merchant!r}; amount cents min/median/max: {lo}/{med}/{hi}
Amounts (cents) and dates: {rows}
Respond with JSON: {{"classification": "bill" | "habit" | "not_recurring"}}"""


class RecurringStats(BaseModel):
    new_series: int = 0
    updated: int = 0
    review: int = 0


def monthly_cents(series: RecurringSeries) -> int:
    return monthlyize(series.expected_cents, series.cadence)


def _stale(last_seen: date, cadence: Cadence, today: date) -> bool:
    """A series is stale once its newest occurrence is over 3 cadence periods old."""
    return (today - last_seen).days > _STALE_PERIODS * CADENCE_DAYS[cadence]


def _median_gap(dates: list[date]) -> float:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    return float(statistics.median(gaps))


def _closest_cadence(dates: list[date]) -> Cadence:
    med = _median_gap(dates)
    return min(CADENCE_DAYS, key=lambda c: abs(CADENCE_DAYS[c] - med))


async def _excluded_txn_ids(session: AsyncSession) -> tuple[set[int], set[int]]:
    """All transfer-linked txns are excluded from merchant grouping. Loan-like payment
    outflows are additionally untagged — their per-account derivation lives in
    queries.loan_payment_stats, not in a merchant series (design 2026-07-16 §3)."""
    excluded: set[int] = set()
    loan_payment_outflows: set[int] = set()
    for link in await classified_links(session):
        excluded.add(link.inflow_id)
        excluded.add(link.outflow_id)
        if link.inflow_is_loan_like:
            loan_payment_outflows.add(link.outflow_id)
    return excluded, loan_payment_outflows


async def detect_recurring(
    session: AsyncSession, llm: Classifier | None, today: date
) -> RecurringStats:
    stats = RecurringStats()
    excluded, loan_payment_outflows = await _excluded_txn_ids(session)
    reviewed: set[tuple[str, str]] = set()
    force: dict[tuple[str, str], tuple[bool, bool]] = {}
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
            force[key] = (
                item.resolution["is_recurring"],
                bool(item.resolution.get("discretionary")),
            )
    existing = {
        (s.merchant, s.direction): s
        for s in (await session.execute(select(RecurringSeries))).scalars()
    }
    # series feed power's income/fixed-cost sums, so they must be single-currency:
    # only the primary currency's transactions can form or update a series
    primary = await primary_currency(session)
    txns = (
        (
            await session.execute(
                select(Transaction)
                .join(Account, Transaction.account_id == Account.id)
                .where(Transaction.merchant.is_not(None), Account.currency == primary)
                .order_by(Transaction.posted_on, Transaction.id)
            )
        )
        .scalars()
        .all()
    )
    # a description correction can re-derive a txn's merchant away from the series it
    # was tagged to — untag it so the old series doesn't keep stale occurrences and the
    # new group sees it; same-merchant corrections stay tagged (so they never look
    # "genuinely new" to the ended-series revival check below)
    series_by_id = {s.id: s for s in existing.values()}
    for t in txns:
        if t.series_id is not None:
            owner = series_by_id.get(t.series_id)
            if owner is not None and owner.merchant != t.merchant:
                t.series_id = None

    # loan-like payment outflows no longer belong to a merchant series — their
    # per-account cadence/amount is derived in queries.loan_payment_stats instead
    # (design 2026-07-16 §3). Untag them and remember which series they leave behind
    # so an orphaned series (no tagged txns left) can be ended in the final sweep.
    untagged_series: set[int] = set()
    for t in txns:
        if t.id in loan_payment_outflows and t.series_id is not None:
            untagged_series.add(t.series_id)
            t.series_id = None

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
        match = match_cadence(dates)
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
        if forced is not None and forced[0] is False:
            continue  # user-resolved as not recurring — suppress silently, forever
        discretionary = forced[1] if forced is not None else False

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
            if forced is None or forced[0] is not True:  # irregular timing needs a human
                # only ask when it plausibly IS a bill: steady amounts at bill-like intervals
                if stable and _median_gap(dates) >= _MIN_REVIEW_GAP_DAYS:
                    _open_review()
                continue
            cadence = _closest_cadence(dates)
        elif not stable and (forced is None or forced[0] is not True):
            answer = (
                await llm.classify_json(
                    _LLM_PROMPT.format(
                        merchant=merchant,
                        lo=min(amounts),
                        med=round(med),
                        hi=max(amounts),
                        rows=[(t.amount_cents, t.posted_on.isoformat()) for t in run],
                    )
                )
                if llm
                else None
            )
            classification = answer.get("classification") if answer else None
            if classification == "habit":
                discretionary = True
            elif classification != "bill":
                _open_review()
                continue
        next_on = advance_expected_on(dates[-1], cadence)
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
                discretionary=discretionary,
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
            if series.status == SeriesStatus.active and status == SeriesStatus.active:
                # a resumed series leaps next_expected_on over unexamined windows here,
                # and events.py only walks forward from the advanced value — emit misses
                # for any empty window the leap skips (revivals deliberately don't burst).
                # bound at the newest charge: windows past it were re-anchored by that
                # charge and belong to events.py's future-guarded loop, not this one
                grace_days = GRACE_DAYS[cadence]
                window = series.next_expected_on
                while window < dates[-1]:
                    if not any(abs((d - window).days) <= grace_days for d in dates):
                        session.add(missed_event(series.id, window))
                    window = advance_expected_on(window, cadence)
            changed = (
                series.next_expected_on != advanced_on
                or series.cadence != cadence
                or series.status != status
                or series.discretionary != discretionary
            )
            series.cadence = cadence
            series.next_expected_on = advanced_on
            series.status = status
            series.discretionary = discretionary
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
        # detect_recurring tags every occurrence it matches, so an active series
        # normally always has tagged txns. Zero tagged txns means either there's no
        # evidence to judge (leave it alone) or every txn it had was just untagged as
        # a loan payment (its per-account derivation owns it now — end it here).
        last_seen = newest_by_series.get(series.id)
        if last_seen is None:
            if series.id in untagged_series:
                series.status = SeriesStatus.ended
                stats.updated += 1
            continue
        if _stale(last_seen, series.cadence, today):
            series.status = SeriesStatus.ended
            stats.updated += 1
    await session.commit()
    return stats
