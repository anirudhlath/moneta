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


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def advance_expected_on(d: date, cadence: Cadence) -> date:
    """One period after d — calendar-aware for monthly/annual so day-of-month holds."""
    match cadence:
        case Cadence.weekly:
            return d + timedelta(days=7)
        case Cadence.biweekly:
            return d + timedelta(days=14)
        case Cadence.monthly:
            return _add_months(d, 1)
        case Cadence.annual:
            return _add_months(d, 12)


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
            start -= 1
        if len(dates) - start < _MIN_OCCURRENCES:
            continue
        if abs(statistics.median(gaps[start:]) - days) <= tol:
            return cadence, dates[start]
    return None


def monthlyize(expected_cents: int, cadence: Cadence) -> int:
    return round(expected_cents * PER_MONTH[cadence])


def monthly_cents(series: RecurringSeries) -> int:
    return monthlyize(series.expected_cents, series.cadence)
