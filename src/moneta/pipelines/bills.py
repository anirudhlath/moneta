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

from moneta.cadence import TOLERANCE, advance_expected_on, match_cadence
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
    """Greedy: each charge extends the track whose next expected date is nearest."""
    tracks: list[list[Transaction]] = []
    for t in _by_date(band):
        same_day = next((tr for tr in tracks if tr[-1].posted_on == t.posted_on), None)
        # a double-post is the same occurrence, unless another track expects a charge
        # around now: then two same-price bills co-posted, and expectation decides
        if same_day is not None and not any(
            abs((t.posted_on - advance_expected_on(tr[-1].posted_on, cadence)).days)
            <= TOLERANCE[cadence]
            for tr in tracks
            if tr is not same_day
        ):
            same_day.append(t)
            continue
        best: list[Transaction] | None = None
        best_gap = TOLERANCE[cadence] + 1
        for track in tracks:
            gap = abs((t.posted_on - advance_expected_on(track[-1].posted_on, cadence)).days)
            if gap < best_gap:
                best, best_gap = track, gap
        if best is None:
            tracks.append([t])
        else:
            best.append(t)
    return tracks


def concurrent_tracks(band: Sequence[Transaction]) -> list[list[Transaction]] | None:
    """The band's simultaneous subscriptions, or None when it is one stream.

    Splits only when every charge sits on a track of 3+ dates and two tracks overlap
    in time — a moved billing day is one track ending where another starts.
    """
    match = match_cadence(_dates(band))
    if match is not None and match[0] in (Cadence.weekly, Cadence.biweekly):
        return None

    # Try monthly; only try annual if monthly produced no valid track (3+ dates).
    monthly_tracks = _tracks(band, Cadence.monthly)
    has_valid_monthly = any(len(_dates(tr)) >= _MIN_TRACK_DATES for tr in monthly_tracks)

    cadences = [Cadence.monthly] if has_valid_monthly else [Cadence.monthly, Cadence.annual]

    for cadence in cadences:
        tracks = monthly_tracks if cadence == Cadence.monthly else _tracks(band, Cadence.annual)

        if len(tracks) < 2 or any(len(_dates(tr)) < _MIN_TRACK_DATES for tr in tracks):
            continue
        if any(
            a[0].posted_on <= b[-1].posted_on and b[0].posted_on <= a[-1].posted_on
            for a, b in itertools.combinations(tracks, 2)
        ):
            return [_by_date(tr) for tr in tracks]
    return None


def _scheduled_units(
    group: Sequence[Transaction],
) -> tuple[list[list[Transaction]], bool]:
    """The group's scheduled units (those with a cadence) and whether anything splits them.

    Splits when two same-price subscriptions run side by side, or when a charge of
    real size (not an adjustment) sits inside a unit's schedule without belonging to it:
    within its newest run, or after it while the next charge is not due yet.
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
        match = match_cadence(dates)
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
    sets the bar even when many small purchases drag the group median down. With no
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
