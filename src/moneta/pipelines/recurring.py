import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import CADENCE_DAYS, GRACE_DAYS, TOLERANCE, advance_expected_on, match_cadence
from moneta.llm import CLASSIFICATION_TAXONOMY, Classifier
from moneta.logical import LogicalData, load_logical_data
from moneta.models import (
    BillRef,
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
    amounts_match,
    bill_ref,
    decision_precedence,
    recurring_cluster_item,
    same_bill,
)
from moneta.pipelines.bills import split_bills, whole_bill
from moneta.pipelines.reconcile import _supersede
from moneta.queries import classified_links, foreign_series_ids, primary_currency

_MIN_OCCURRENCES = 3
_AMOUNT_TOLERANCE = 0.20
_STALE_PERIODS = 3
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


_LLM_PROMPT = (
    "Classify this group of bank transactions from one merchant.\n"
    f"{CLASSIFICATION_TAXONOMY}\n"
    "Merchant: {merchant!r}; amount cents min/median/max: {lo}/{med}/{hi}\n"
    "Amounts (cents) and dates: {rows}\n"
    'Respond with JSON: {{"classification": "bill" | "habit" | "not_recurring"}}'
)


class RecurringStats(BaseModel):
    new_series: int = 0
    updated: int = 0
    review: int = 0
    merged: int = 0


class ForcedAnswer(NamedTuple):
    """A resolved recurring_cluster ReviewItem's answer for one bill."""

    is_recurring: bool
    discretionary: bool


def _stale(last_seen: date, cadence: Cadence, today: date) -> bool:
    """A series is stale once its newest occurrence is over 3 cadence periods old."""
    return (today - last_seen).days > _STALE_PERIODS * CADENCE_DAYS[cadence]


def _median_gap(dates: list[date]) -> float:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    return float(statistics.median(gaps))


def _closest_cadence(dates: list[date]) -> Cadence:
    """Forced-in groups have no clean run; the newest gap is the least-stale evidence.
    A between-buckets median once billed a $265.72 gym at biweekly ($575/mo)."""
    if len(dates) < 2:
        return Cadence.monthly
    newest_gap = (dates[-1] - dates[-2]).days
    candidate = min(CADENCE_DAYS, key=lambda c: abs(CADENCE_DAYS[c] - newest_gap))
    if abs(CADENCE_DAYS[candidate] - newest_gap) <= TOLERANCE[candidate]:
        return candidate
    return Cadence.monthly  # safest: factor 1.0 can't inflate the monthly number


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


@dataclass
class _Run:
    """State shared by every bill one detect_recurring call examines."""

    session: AsyncSession
    llm: Classifier | None
    today: date
    stats: RecurringStats
    logical: LogicalData
    foreign_series: set[int]
    open_refs: list[BillRef]
    resolved: list[tuple[BillRef, int, ForcedAnswer]]
    primary: str


def _forced(resolved: list[tuple[BillRef, int, ForcedAnswer]], ref: BillRef) -> ForcedAnswer | None:
    """The decision in force for a bill: amount-specific beats merchant-level, then the
    newest (design 2026-10-13 §4)."""
    covering = [
        (decision_precedence(r, item_id), answer)
        for r, item_id, answer in resolved
        if same_bill(r, ref)
    ]
    return max(covering, key=lambda c: c[0])[1] if covering else None


def _claim(
    unclaimed: list[RecurringSeries],
    bill: Sequence[Transaction],
    amount: int,
    foreign: set[int],
) -> RecurringSeries | None:
    """The series tagging the bill's newest tagged charge, else the nearest amount within
    20% (design 2026-10-13 §3). Claimed series leave the pool."""
    by_id = {s.id: s for s in unclaimed}
    chosen = next((by_id[t.series_id] for t in reversed(bill) if t.series_id in by_id), None)
    if chosen is None:
        near = [
            s
            for s in unclaimed
            if s.id not in foreign and amounts_match(abs(s.expected_cents), amount)
        ]
        chosen = min(near, key=lambda s: (abs(abs(s.expected_cents) - amount), s.id), default=None)
    if chosen is None:
        # a series priced in another currency has its tags outside this primary-currency
        # bill and an amount that can't be compared, so merchant and direction alone
        # identify it, as before per-bill series; the caller rebuilds it in this currency
        chosen = next((s for s in unclaimed if s.id in foreign), None)
    if chosen is not None:
        unclaimed.remove(chosen)
    return chosen


async def _fold(session: AsyncSession, loser: RecurringSeries, survivor: RecurringSeries) -> None:
    """Collapse a pre-merge duplicate into the series its charges now feed."""
    await session.flush()  # survivor tags are written before the loser's are cleared
    await session.execute(
        update(SeriesEvent).where(SeriesEvent.series_id == loser.id).values(series_id=survivor.id)
    )
    await session.execute(
        update(Transaction).where(Transaction.series_id == loser.id).values(series_id=None)
    )
    for item in (
        await session.execute(select(ReviewItem).where(ReviewItem.kind == ReviewKind.price_change))
    ).scalars():
        if item.payload.get("series_id") == loser.id:
            item.payload = {**item.payload, "series_id": survivor.id}
    await session.delete(loser)


async def _detect_bill(
    ctx: _Run,
    merchant: str,
    direction: Direction,
    significant: list[Transaction],
    *,
    split: bool,
    unclaimed: list[RecurringSeries],
) -> tuple[RecurringSeries, set[int]] | None:
    """One candidate bill through cadence, stability, decisions and series matching.
    Returns the series it feeds and the series ids its charges were tagged to before."""
    if len(significant) < _MIN_OCCURRENCES:
        return None
    voted = {t.series_id for t in significant if t.series_id is not None}
    dates = sorted({t.posted_on for t in significant})  # dedup: double-posts aren't 0-day gaps
    match = match_cadence(dates)
    if match is None:
        cadence, recent = None, significant
    else:
        # stats come from the newest cadence-run so ancient price epochs don't skew them
        cadence, run_start = match
        recent = [t for t in significant if t.posted_on >= run_start]
    amounts = [abs(t.amount_cents) for t in recent]
    med = statistics.median(amounts)
    stable = all(abs(a - med) <= med * _AMOUNT_TOLERANCE for a in amounts)
    expected = -round(med) if direction == Direction.outflow else round(med)
    ref: BillRef = (merchant, str(direction), round(med))
    forced = _forced(ctx.resolved, ref)
    if forced is not None and forced.is_recurring is False:
        return None  # user-resolved as not recurring — suppress silently, forever
    discretionary = forced.discretionary if forced is not None else False
    forced_recurring = forced is not None and forced.is_recurring

    def _open_review(*, llm_flagged: bool = False) -> None:
        if any(same_bill(r, ref) for r in ctx.open_refs):
            return
        # a question names the amount only when the merchant has several bills
        amount = round(med) if split else None
        item = recurring_cluster_item(merchant, direction, amount)
        if llm_flagged:
            item.payload = {**item.payload, "llm_flagged": True}
        ctx.session.add(item)
        ctx.open_refs.append((merchant, str(direction), amount))
        ctx.stats.review += 1

    if cadence is None:
        if not forced_recurring:  # irregular timing needs a human
            # only ask when it plausibly IS a bill: steady amounts at bill-like intervals
            # (a single date is never bill-like timing — nothing to measure a gap from)
            if stable and len(dates) >= 2 and _median_gap(dates) >= _MIN_REVIEW_GAP_DAYS:
                _open_review()
            return None
        cadence = _closest_cadence(dates)
    elif not stable and not forced_recurring:
        answer = (
            await ctx.llm.classify_json(
                _LLM_PROMPT.format(
                    merchant=merchant,
                    lo=min(amounts),
                    med=round(med),
                    hi=max(amounts),
                    rows=[(t.amount_cents, t.posted_on.isoformat()) for t in recent],
                )
            )
            if ctx.llm
            else None
        )
        classification = answer.get("classification") if answer else None
        if classification == "habit":
            discretionary = True
        elif classification != "bill":
            # an actual (even malformed) LLM answer means the LLM already looked —
            # mirror verify_series: human-only from here, autoreview must not re-ask
            # the binary bill/one-off prompt and rubber-stamp it as a fixed cost.
            # A classify_json failure (answer is None) never looked; leave it askable.
            _open_review(llm_flagged=answer is not None)
            return None
    next_on = advance_expected_on(dates[-1], cadence)
    stale = _stale(dates[-1], cadence, ctx.today)
    series = _claim(unclaimed, significant, round(med), ctx.foreign_series)
    session, logical, stats = ctx.session, ctx.logical, ctx.stats
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
        changing_currency = series.id in ctx.foreign_series
        if changing_currency:
            # Merchant/direction is the existing series identity. Rebuild its
            # monetary state from this currency and retire only derived tags;
            # source observations and classification history stay untouched.
            previous_currencies = {
                logical.accounts[logical.account_roots[txn.account_id]].currency
                for txn in logical.transactions
                if txn.series_id == series.id
            }
            for item in await session.scalars(
                select(ReviewItem).where(
                    ReviewItem.kind == ReviewKind.price_change,
                    ReviewItem.status == ReviewStatus.open,
                )
            ):
                if item.payload.get("series_id") == series.id and "currency" not in item.payload:
                    item.payload = {
                        **item.payload,
                        "currency": (
                            next(iter(previous_currencies))
                            if len(previous_currencies) == 1
                            else "ambiguous"
                        ),
                    }
            for txn in logical.transactions:
                if (
                    txn.series_id == series.id
                    and logical.accounts[logical.account_roots[txn.account_id]].currency
                    != ctx.primary
                ):
                    txn.series_id = None
            series.expected_cents = expected
            ctx.foreign_series.discard(series.id)
        advanced_on = max(series.next_expected_on, next_on)
        if stale:
            status = SeriesStatus.ended
        elif (
            series.status == SeriesStatus.ended
            and significant[-1].series_id is None
            and not changing_currency
        ):
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
            changing_currency
            or series.next_expected_on != advanced_on
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
    return series, voted


async def detect_recurring(
    session: AsyncSession, llm: Classifier | None, today: date
) -> RecurringStats:
    stats = RecurringStats()
    excluded, loan_payment_outflows = await _excluded_txn_ids(session)
    # bill-scoped: an answer about one bill (direction and, when named, amount) must
    # not force another (design 2026-10-13 §4)
    open_refs: list[BillRef] = []
    # open merchant-level questions, retired when their merchant splits into bills
    open_merchant_level: dict[tuple[str, str], list[ReviewItem]] = {}
    resolved: list[tuple[BillRef, int, ForcedAnswer]] = []
    for item in (
        await session.execute(
            select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
        )
    ).scalars():
        ref = bill_ref(item.payload)
        if ref is None:
            continue
        if item.status == ReviewStatus.open:
            open_refs.append(ref)
            if ref[2] is None:
                open_merchant_level.setdefault((ref[0], ref[1]), []).append(item)
        elif isinstance(item.resolution, dict) and isinstance(
            item.resolution.get("is_recurring"), bool
        ):
            resolved.append(
                (
                    ref,
                    item.id,
                    ForcedAnswer(
                        is_recurring=item.resolution["is_recurring"],
                        discretionary=bool(item.resolution.get("discretionary")),
                    ),
                )
            )
    all_series = list(
        (await session.execute(select(RecurringSeries).order_by(RecurringSeries.id))).scalars()
    )
    pool: dict[tuple[str, str], list[RecurringSeries]] = {}
    for s in all_series:
        pool.setdefault((s.merchant, str(s.direction)), []).append(s)
    # series feed power's income/fixed-cost sums, so they must be single-currency:
    # only the primary currency's transactions can form or update a series
    primary = await primary_currency(session)
    logical = await load_logical_data(session)
    foreign_series = foreign_series_ids(logical, primary)
    txns = sorted(
        (
            t
            for t in logical.transactions
            if t.merchant is not None
            and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        ),
        key=lambda t: (t.posted_on, t.id),
    )
    # series left with no tagged txns this run (loan payments, a rename that moved every
    # charge away) are ended by the final sweep
    untagged_series: set[int] = set()
    # a description correction can re-derive a txn's merchant away from the series it
    # was tagged to — untag it so the old series doesn't keep stale occurrences and the
    # new group sees it; same-merchant corrections stay tagged (so they never look
    # "genuinely new" to the ended-series revival check)
    series_by_id = {s.id: s for s in all_series}
    for t in txns:
        if t.series_id is not None:
            owner = series_by_id.get(t.series_id)
            if owner is not None and owner.merchant != t.merchant:
                untagged_series.add(owner.id)
                t.series_id = None

    # loan-like payment outflows no longer belong to a merchant series — their
    # per-account cadence/amount is derived in queries.loan_payment_stats instead
    # (design 2026-07-16 §3).
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

    ctx = _Run(session, llm, today, stats, logical, foreign_series, open_refs, resolved, primary)
    folded: set[int] = set()
    for (merchant, direction), group in groups.items():
        bills = split_bills(group)
        split = bills is not None
        if split:
            # a question asked before the split covers every bill, so answering "no" to
            # it would suppress the subscription; each bill is asked about on its own
            for item in open_merchant_level.pop((merchant, str(direction)), []):
                _supersede(item, "Merchant now has several bills, asked about one by one", today)
                open_refs.remove((merchant, str(direction), None))
        if bills is None:
            # one bill, handled as before: drop sub-scale adjustments
            bills = [whole_bill(group)]
        unclaimed = list(pool.get((merchant, str(direction)), []))
        claims: list[tuple[RecurringSeries, list[Transaction], set[int]]] = []
        for bill in sorted(bills, key=len, reverse=True):
            result = await _detect_bill(
                ctx, merchant, direction, bill, split=split, unclaimed=unclaimed
            )
            if result is not None:
                claims.append((result[0], bill, result[1]))
        for series, bill, voted in claims:
            members = {t.id for t in bill}
            for t in group:  # top-ups and orders leave the subscription they rode on
                if t.series_id == series.id and t.id not in members:
                    t.series_id = None
            for loser in [s for s in unclaimed if s.id in voted]:
                unclaimed.remove(loser)
                await _fold(session, loser, series)
                folded.add(loser.id)
                stats.merged += 1

    # Groups that no longer match a cadence (trailing noise, shrunk by exclusions) never
    # reach the stale check above — sweep every still-active series by its own txns.
    newest_by_series: dict[int, date] = {}
    for txn in logical.transactions:
        if txn.series_id is not None:
            newest_by_series[txn.series_id] = max(
                newest_by_series.get(txn.series_id, txn.posted_on), txn.posted_on
            )
    for series in all_series:
        if series.id in folded:
            continue
        if series.status != SeriesStatus.active or series.id in foreign_series:
            continue
        # detect_recurring tags every occurrence it matches, so an active series
        # normally always has tagged txns. Zero tagged txns means either there's no
        # evidence to judge (leave it alone) or every txn it had was just untagged
        # (loan payments, merchant moved) — end it here.
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
