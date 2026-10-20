import itertools
import statistics
from calendar import monthrange
from datetime import date, timedelta

from moneta.models import Cadence, RecurringSeries

CADENCE_DAYS: dict[Cadence, int] = {
    Cadence.weekly: 7,
    Cadence.biweekly: 14,
    Cadence.monthly: 30,
    Cadence.annual: 365,
}
TOLERANCE: dict[Cadence, int] = {
    Cadence.weekly: 2,
    Cadence.biweekly: 3,
    Cadence.monthly: 6,
    Cadence.annual: 20,
}
# days around next_expected_on within which a charge counts as "on time"
GRACE_DAYS: dict[Cadence, int] = {
    Cadence.weekly: 3,
    Cadence.biweekly: 4,
    Cadence.monthly: 7,
    Cadence.annual: 30,
}
PER_MONTH: dict[Cadence, float] = {
    Cadence.weekly: 52 / 12,
    Cadence.biweekly: 26 / 12,
    Cadence.monthly: 1.0,
    Cadence.annual: 1 / 12,
}

_MIN_OCCURRENCES = 3


def month_bounds(d: date) -> tuple[date, date]:
    """First and last day of d's calendar month."""
    return date(d.year, d.month, 1), date(d.year, d.month, monthrange(d.year, d.month)[1])


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def periods_after(d: date, cadence: Cadence, n: int) -> date:
    """n periods after d — calendar-aware for monthly/annual so day-of-month holds."""
    match cadence:
        case Cadence.weekly:
            return d + timedelta(days=7 * n)
        case Cadence.biweekly:
            return d + timedelta(days=14 * n)
        case Cadence.monthly:
            return _add_months(d, n)
        case Cadence.annual:
            return _add_months(d, 12 * n)


def advance_expected_on(d: date, cadence: Cadence) -> date:
    """One period after d."""
    return periods_after(d, cadence, 1)


def phase_spread(dates: list[date], cadence: Cadence) -> int:
    """How far consecutive charges, one period apart, drift from a single schedule: the
    spread in days of each date's offset from the slot n periods after the first. Every
    date within TOLERANCE of one schedule keeps it at or under twice TOLERANCE."""
    offsets = [(d - periods_after(dates[0], cadence, n)).days for n, d in enumerate(dates)]
    return max(offsets) - min(offsets)


def grid_spread(dates: list[date], cadence: Cadence) -> int:
    """The fewest days of a weekly or biweekly period that hold every date's place in it:
    small when the dates keep one fixed schedule, even across skipped periods."""
    days = CADENCE_DAYS[cadence]
    places = sorted({(d - dates[0]).days % days for d in dates})
    return days - max(b - a for a, b in itertools.pairwise([*places, places[0] + days]))


def match_cadence(dates: list[date]) -> tuple[Cadence, date] | None:
    """Best cadence and the start date of the newest run matching it.

    Deep history contains breaks (pauses, resubscriptions, card reissues); judging
    cadence on the maximal recent run keeps ancient gaps from poisoning a
    currently-clean series.
    """
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    for cadence, days in CADENCE_DAYS.items():
        tol = TOLERANCE[cadence]
        start = len(dates) - 1
        while start > 0 and abs(gaps[start - 1] - days) <= tol * 2:
            # a biweekly run keeps one fixed schedule too: two monthly bills half a month
            # apart pass every gap test, but drift off a 14-day schedule
            if cadence == Cadence.biweekly and phase_spread(dates[start - 1 :], cadence) > tol * 2:
                break
            start -= 1
        if len(dates) - start < _MIN_OCCURRENCES:
            continue
        if abs(statistics.median(gaps[start:]) - days) <= tol:
            return cadence, dates[start]
    return None


def match_across_a_miss(dates: list[date]) -> tuple[Cadence, date, date] | None:
    """A monthly run that holds only across one missed month: the cadence, the run's start
    and the date of the charge before the miss. None when no such run reaches 3 dates.

    `match_cadence` ends a run at the first gap it cannot place, so one month paid from
    elsewhere cuts a schedule down to the charges after it.
    """
    days, tol = CADENCE_DAYS[Cadence.monthly], TOLERANCE[Cadence.monthly]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    start, missed = len(dates) - 1, None
    while start > 0:
        gap = gaps[start - 1]
        if missed is None and abs(gap - 2 * days) <= tol * 2:
            missed = start - 1
        elif abs(gap - days) > tol * 2:
            break
        start -= 1
    if missed is None or len(dates) - start < _MIN_OCCURRENCES:
        return None
    periods = [gap / 2 if i == missed else gap for i, gap in enumerate(gaps) if i >= start]
    if abs(statistics.median(periods) - days) > tol:
        return None
    return Cadence.monthly, dates[start], dates[missed]


def monthlyize(expected_cents: int, cadence: Cadence) -> int:
    return round(expected_cents * PER_MONTH[cadence])


def monthly_cents(series: RecurringSeries) -> int:
    return monthlyize(series.expected_cents, series.cadence)
