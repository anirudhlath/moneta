"""Split one merchant's charges into bills (design 2026-10-13 §2). Pure — no I/O.

A merchant can carry several bills (a subscription and its top-ups, two same-price
subscriptions, two income streams from one payer). Splitting happens only when a
schedule has extra charges alongside it; otherwise the group stays one bill and
detection handles it exactly as before, so a variable bill is never dropped.
"""

import itertools
import statistics
from collections.abc import Sequence
from datetime import date, timedelta

from moneta.cadence import (
    CADENCE_DAYS,
    TOLERANCE,
    advance_expected_on,
    grid_spread,
    match_across_a_miss,
    match_cadence,
    phase_spread,
)
from moneta.models import BILL_TOLERANCE, Cadence, Transaction

_MIN_TRACK_DATES = 3
# charges below this fraction of a bill's median are adjustments, not occurrences
MINOR_FRACTION = 0.25


def _by_date(txns: Sequence[Transaction]) -> list[Transaction]:
    return sorted(txns, key=lambda t: (t.posted_on, t.id))


def _dates(txns: Sequence[Transaction]) -> list[date]:
    return sorted({t.posted_on for t in txns})


def amount_bands(txns: Sequence[Transaction]) -> list[list[Transaction]]:
    """Chain charges by size: each joins the band of the next-smaller charge when at
    most 20% above it."""
    bands: list[list[Transaction]] = []
    for t in sorted(txns, key=lambda t: (abs(t.amount_cents), t.posted_on, t.id)):
        if bands and abs(t.amount_cents) <= abs(bands[-1][-1].amount_cents) * (1 + BILL_TOLERANCE):
            bands[-1].append(t)
        else:
            bands.append([t])
    return [_by_date(b) for b in bands]


def _tracks(band: Sequence[Transaction], cadence: Cadence) -> list[list[Transaction]]:
    """Greedy: each charge extends the track whose next expected date is nearest, the
    nearer price breaking a tie. A track takes one charge a day, bar a double-post."""
    tracks: list[list[Transaction]] = []
    for t in _by_date(band):
        same_day = next(
            (
                tr
                for tr in tracks
                if tr[-1].posted_on == t.posted_on and tr[-1].amount_cents == t.amount_cents
            ),
            None,
        )
        # a double-post repeats the charge's price on its day and is the same occurrence,
        # unless another track expects a charge around now: then two same-price bills
        # co-posted, and expectation decides
        if same_day is not None and not any(
            abs((t.posted_on - advance_expected_on(tr[-1].posted_on, cadence)).days)
            <= TOLERANCE[cadence]
            for tr in tracks
            if tr is not same_day
        ):
            same_day.append(t)
            continue
        best: list[Transaction] | None = None
        best_key = (TOLERANCE[cadence] + 1, 0)
        for track in tracks:
            if track[-1].posted_on == t.posted_on:
                continue
            gap = abs((t.posted_on - advance_expected_on(track[-1].posted_on, cadence)).days)
            key = (gap, abs(t.amount_cents - track[-1].amount_cents))
            if key < best_key:
                best, best_key = track, key
        if best is None:
            tracks.append([t])
        else:
            best.append(t)
    return tracks


def _bill_track(track: Sequence[Transaction], cadence: Cadence) -> bool:
    """A track that is a bill: 3+ dates on one schedule (`phase_spread` within twice
    TOLERANCE) at one price (every charge within 20% of the track's median)."""
    dates = _dates(track)
    if len(dates) < _MIN_TRACK_DATES or phase_spread(dates, cadence) > 2 * TOLERANCE[cadence]:
        return False
    median = statistics.median(abs(t.amount_cents) for t in track)
    return all(abs(abs(t.amount_cents) - median) <= median * BILL_TOLERANCE for t in track)


def _one_fixed_schedule(bills: Sequence[Sequence[Transaction]]) -> bool:
    """Monthly tracks that are one weekly or biweekly stream with skipped periods (a
    paycheck missing a payday chains into drifting monthly tracks): every charge's place
    in the period lies within TOLERANCE days of the others' (a fixed payday barely moves,
    while a monthly bill moves two or three days along a fixed period every month), and no
    two come closer than a period less twice its tolerance, so two bills on one day are
    never one stream."""
    dates = sorted(d for tr in bills for d in _dates(tr))
    closest = min((b - a).days for a, b in itertools.pairwise(dates))
    return any(
        closest >= CADENCE_DAYS[cadence] - 2 * TOLERANCE[cadence]
        and grid_spread(dates, cadence) <= TOLERANCE[cadence]
        for cadence in (Cadence.weekly, Cadence.biweekly)
    )


def concurrent_tracks(band: Sequence[Transaction]) -> list[list[Transaction]] | None:
    """The band's simultaneous bills, or None when it is one stream.

    Splits when two bill tracks (`_bill_track`) overlap in time — a moved billing day is
    one track ending where another starts — and charges on no bill track number at most
    half the bills' charges between the first bill's start and the last one's end. Those
    one-off charges are left out of every track, so they stay spending. A band whose
    newest weekly or biweekly run holds most of its dates is one stream, and so are
    monthly bill tracks that keep one fixed schedule (`_one_fixed_schedule`).
    """
    dates = _dates(band)
    match = match_cadence(dates)
    # a short run at the tail of several monthly bills is a coincidence of their days
    if (
        match is not None
        and match[0] in (Cadence.weekly, Cadence.biweekly)
        and 2 * sum(1 for d in dates if d >= match[1]) > len(dates)
    ):
        return None

    # Try monthly; only try annual if monthly produced no track of 3+ dates.
    monthly_tracks = _tracks(band, Cadence.monthly)
    has_valid_monthly = any(len(_dates(tr)) >= _MIN_TRACK_DATES for tr in monthly_tracks)

    cadences = [Cadence.monthly] if has_valid_monthly else [Cadence.monthly, Cadence.annual]

    for cadence in cadences:
        tracks = monthly_tracks if cadence == Cadence.monthly else _tracks(band, Cadence.annual)
        bills = [tr for tr in tracks if _bill_track(tr, cadence)]
        if not any(
            a[0].posted_on <= b[-1].posted_on and b[0].posted_on <= a[-1].posted_on
            for a, b in itertools.combinations(bills, 2)
        ):
            continue
        if cadence == Cadence.monthly and _one_fixed_schedule(bills):
            return None
        start, end = min(tr[0].posted_on for tr in bills), max(tr[-1].posted_on for tr in bills)
        on_bills = {t.id for tr in bills for t in tr}
        one_offs = sum(1 for t in band if start <= t.posted_on <= end and t.id not in on_bills)
        if 2 * one_offs <= len(on_bills):
            return [_by_date(tr) for tr in bills]
    return None


def _across_a_miss(
    unit: Sequence[Transaction], group: Sequence[Transaction]
) -> tuple[Cadence, date] | None:
    """A unit's monthly schedule across one missed month (an installment paid from an
    account that isn't linked). Not when the merchant charged anything around the missed
    due date: that is the month's bill at another size. Not when two of the unit's
    charges are less than 18 days apart: a habit that repeats a price is not a schedule.
    Not when the merchant kept charging at real size through the gap (more than one
    charge of at least `MINOR_FRACTION` of the unit's median): a missed installment
    leaves the merchant quiet."""
    dates = _dates(unit)
    bridged = match_across_a_miss(dates)
    if bridged is None:
        return None
    cadence, start, before = bridged
    missed_on = advance_expected_on(before, cadence)
    if any(abs((t.posted_on - missed_on).days) <= TOLERANCE[cadence] for t in group):
        return None
    shortest = CADENCE_DAYS[cadence] - 2 * TOLERANCE[cadence]
    if any((b - a).days < shortest for a, b in itertools.pairwise(dates)):
        return None
    after = dates[dates.index(before) + 1]
    floor = statistics.median(abs(t.amount_cents) for t in unit) * MINOR_FRACTION
    busy = sum(1 for t in group if before < t.posted_on < after and abs(t.amount_cents) >= floor)
    return None if busy > 1 else (cadence, start)


def _scheduled_units(
    group: Sequence[Transaction],
) -> tuple[list[list[Transaction]], bool]:
    """The group's scheduled units (those with a cadence) and whether anything splits them.

    Splits when two same-price subscriptions run side by side, or when a charge of
    real size (not an adjustment) sits inside a unit's schedule without belonging to it:
    within its newest run, or after it while the next charge is not due yet. A run holds
    across one missed month (`_across_a_miss`).
    """
    units: list[list[Transaction]] = []
    concurrent = False
    for band in amount_bands(group):
        tracks = concurrent_tracks(band)
        if tracks is None:
            units.append(band)
        else:
            concurrent = True
            units.extend(tracks)
    scheduled: list[list[Transaction]] = []
    extras = False
    for unit in units:
        dates = _dates(unit)
        match = match_cadence(dates) or _across_a_miss(unit, group)
        if match is None:
            continue
        scheduled.append(unit)
        members = {t.id for t in unit}
        (cadence, start), end = match, dates[-1]
        # nothing charged since the next charge came due: a later charge (a term deposit
        # after the newest installment) sits alongside the schedule too
        due = advance_expected_on(end, cadence) - timedelta(days=TOLERANCE[cadence])
        if all(t.posted_on < due for t in group):
            end = max(t.posted_on for t in group)
        # a charge far below the bill is an adjustment (a fee, a true-up), not a second bill
        floor = statistics.median(abs(t.amount_cents) for t in unit) * MINOR_FRACTION
        extras = extras or any(
            start <= t.posted_on <= end and t.id not in members and abs(t.amount_cents) >= floor
            for t in group
        )
    return scheduled, concurrent or extras


def split_bills(group: Sequence[Transaction]) -> list[list[Transaction]] | None:
    """Candidate bills for one (merchant, direction) group, or None to keep it whole."""
    scheduled, splits = _scheduled_units(group)
    return scheduled if scheduled and splits else None


def whole_bill(group: Sequence[Transaction]) -> list[Transaction]:
    """The group as one bill: its charges minus adjustments and small purchases.

    "Small" is measured against the scheduled unit that declined to split (the one
    with the most dates; ties go to the larger charge), so a subscription's own size
    sets the bar even when many small purchases drag the group median down. A unit that
    holds across a missed month counts as scheduled, so it can set the bar too. With no
    scheduled unit the group's median is the bar.
    """
    scheduled, _ = _scheduled_units(group)
    if scheduled:
        unit = max(
            scheduled,
            key=lambda u: (len(_dates(u)), statistics.median(abs(t.amount_cents) for t in u)),
        )
        scale = statistics.median(abs(t.amount_cents) for t in unit)
    else:
        scale = statistics.median(abs(t.amount_cents) for t in group)
    return [t for t in group if abs(t.amount_cents) >= scale * MINOR_FRACTION]
