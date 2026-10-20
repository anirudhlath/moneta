from datetime import date, timedelta

from moneta.cadence import (
    TOLERANCE,
    advance_expected_on,
    grid_spread,
    match_across_a_miss,
    match_cadence,
    phase_spread,
)
from moneta.models import Cadence, Transaction
from moneta.pipelines.bills import amount_bands, concurrent_tracks, split_bills, whole_bill

_ids = iter(range(1, 10_000))


def _t(posted_on: date, cents: int) -> Transaction:
    return Transaction(id=next(_ids), posted_on=posted_on, amount_cents=cents, series_id=None)


def _monthly(day: int, months: range, cents: int, year: int = 2026) -> list[Transaction]:
    return [_t(date(year, m, day), cents) for m in months]


def test_amount_bands_chain_within_twenty_percent() -> None:
    txns = [_t(date(2026, 1, d), c) for d, c in ((1, -1000), (2, -1150), (3, -1300), (4, -2600))]
    bands = amount_bands(txns)
    assert [[abs(t.amount_cents) for t in b] for b in bands] == [[1000, 1150, 1300], [2600]]


def test_concurrent_same_price_subscriptions_become_tracks() -> None:
    band = sorted(
        _monthly(11, range(1, 7), -2000) + _monthly(15, range(4, 7), -2000),
        key=lambda t: t.posted_on,
    )
    tracks = concurrent_tracks(band)
    assert tracks is not None
    assert sorted(len(tr) for tr in tracks) == [3, 6]


def test_moved_billing_day_is_one_stream() -> None:
    band = _monthly(11, range(1, 4), -1500) + _monthly(18, range(4, 7), -1500)
    assert concurrent_tracks(band) is None


def test_a_weekly_stream_is_not_split_into_tracks() -> None:
    band = [_t(date(2026, 1, 5) + timedelta(days=7 * i), -500) for i in range(12)]
    assert concurrent_tracks(band) is None


def test_a_weekly_stream_that_skipped_a_week_is_not_split_into_monthly_bills() -> None:
    # the skip leaves a short weekly run, and the weeks chain into four drifting monthly
    # tracks, but every charge still sits on one 7-day schedule
    weeks = [date(2026, 1, 6) + timedelta(days=7 * i) for i in range(17)]
    weeks.remove(date(2026, 4, 7))
    assert split_bills([_t(d, -2500) for d in weeks]) is None


def _pair(first: tuple[int, int], second: tuple[int, int], months: int) -> list[Transaction]:
    (day_a, cents_a), (day_b, cents_b) = first, second
    pair = [
        *_monthly(day_a, range(1, months + 1), cents_a),
        *_monthly(day_b, range(1, months + 1), cents_b),
    ]
    return sorted(pair, key=lambda t: (t.posted_on, t.id))


def test_bills_a_few_days_apart_split_after_three_months() -> None:
    # no fixed weekly or biweekly stream posts two charges on one day or two days apart
    assert split_bills(_pair((11, -1000), (11, -1150), 3)) is not None
    assert split_bills(_pair((11, -2000), (13, -2000), 3)) is not None


def test_bills_half_a_month_apart_split_once_they_drift_off_a_fortnight() -> None:
    # for five months the 9th and the 24th keep a 14-day schedule as well as a fortnightly
    # bill would; by the sixth they have drifted off it
    assert split_bills(_pair((9, -4700), (24, -5300), 5)) is None
    assert split_bills(_pair((9, -4700), (24, -5300), 6)) is not None


def test_phase_spread_measures_drift_from_one_schedule() -> None:
    fortnights = [date(2026, 1, 2) + timedelta(days=14 * i) for i in range(6)]
    assert phase_spread(fortnights, Cadence.biweekly) == 0
    # a calendar month keeps its day, clamped through a short month
    month_ends = [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    assert phase_spread(month_ends, Cadence.monthly) == 0
    jittered = [date(2026, 1, 2), date(2026, 1, 14), date(2026, 2, 1)]
    assert phase_spread(jittered, Cadence.biweekly) == 4


def test_grid_spread_holds_a_fixed_schedule_across_skipped_periods() -> None:
    # fortnights with two skipped, one a day late: every date keeps its place in the period
    fortnights = [date(2026, 1, 2) + timedelta(days=14 * i) for i in (0, 1, 3, 4, 7)]
    fortnights[2] += timedelta(days=1)
    assert grid_spread(fortnights, Cadence.biweekly) == 1
    # places 13 and 0 sit a day apart across the period's end
    assert grid_spread([date(2026, 1, 2), date(2026, 1, 29)], Cadence.biweekly) == 1
    # a day of the month moves two or three days along a 14-day schedule every month
    months = [date(2026, m, 9) for m in range(1, 7)]
    assert grid_spread(months, Cadence.biweekly) > 2 * TOLERANCE[Cadence.biweekly]


def test_a_biweekly_run_keeps_one_fixed_schedule() -> None:
    # bills on the 9th and the 24th pass every fortnightly gap test, but slip a day or two
    # off a 14-day schedule each month: the run holds only the newest charges that keep one
    halves = [date(2026, month, day) for month in range(1, 7) for day in (9, 24)]
    assert match_cadence(halves) == (Cadence.biweekly, date(2026, 4, 9))


def test_a_biweekly_stream_with_shifted_paydays_keeps_its_whole_run() -> None:
    # a payday moved a day or two early for a holiday is still on the same schedule
    paydays = [date(2026, 1, 2) + timedelta(days=14 * i) for i in range(12)]
    paydays[3] -= timedelta(days=2)
    paydays[8] -= timedelta(days=1)
    assert match_cadence(paydays) == (Cadence.biweekly, paydays[0])


def _ids_of(bills: list[list[Transaction]] | None) -> list[list[int]]:
    assert bills is not None
    return sorted([t.id for t in b] for b in bills)


def test_monthly_bills_half_a_month_apart_split_into_two_bills() -> None:
    # two monthly bills at prices within 20% read as one biweekly stream; each keeps its own
    # day of the month, and a one-off charge between them is no bill
    early = _monthly(9, range(3, 10), -4700)
    late = _monthly(24, range(3, 10), -5300)
    one_off = _t(date(2026, 6, 2), -5000)
    bills = split_bills(sorted([*early, *late, one_off], key=lambda t: t.posted_on))
    assert _ids_of(bills) == _ids_of([early, late])


def test_mixed_price_subscriptions_split_despite_a_weekly_looking_tail() -> None:
    # three subscriptions whose prices chain into one band, two of them billed the same
    # day; a one-off charge between them makes the newest three dates read weekly
    storage = _monthly(11, range(4, 11), -1000)
    music = _monthly(11, range(6, 11), -1150)
    video = _monthly(28, range(4, 10), -1300)
    one_off = _t(date(2026, 10, 2), -1100)
    group = sorted([*storage, *music, *video, one_off], key=lambda t: (t.posted_on, t.id))
    assert _ids_of(split_bills(group)) == _ids_of([storage, music, video])


def test_same_day_charges_at_different_prices_are_not_a_double_post() -> None:
    # two subscriptions billed the same day; after the first month the dearer one posts
    # first, and the nearer price, not the posting order, picks its track
    cheap: list[Transaction] = []
    dear: list[Transaction] = []
    for month in range(4, 9):
        pair = ((-1000, cheap), (-1150, dear)) if month == 4 else ((-1150, dear), (-1000, cheap))
        for cents, bill in pair:
            bill.append(_t(date(2026, month, 11), cents))
    group = sorted([*cheap, *dear], key=lambda t: (t.posted_on, t.id))
    assert _ids_of(concurrent_tracks(group)) == _ids_of([cheap, dear])


def test_pay_on_the_first_and_the_fifteenth_is_two_monthly_streams() -> None:
    # twice a month is 24 paydays a year, not a fortnightly 26
    first = _monthly(1, range(1, 10), 150000)
    fifteenth = _monthly(15, range(1, 10), 150000)
    bills = split_bills(sorted([*first, *fifteenth], key=lambda t: t.posted_on))
    assert _ids_of(bills) == _ids_of([first, fifteenth])


def test_a_biweekly_stream_that_skipped_paydays_is_not_split_into_monthly_bills() -> None:
    # two paydays went out at another amount, so this band skips them: its fortnights then
    # chain into monthly tracks that drift, but every charge still sits on one 14-day grid
    paydays = [date(2026, 1, 2) + timedelta(days=14 * i) for i in range(20)]
    group = [_t(d, 90000 if i in (6, 13) else 180000 + 1000 * i) for i, d in enumerate(paydays)]
    assert split_bills(group) is None


def test_one_off_charges_may_number_at_most_half_the_bills_charges() -> None:
    # two bills on the 5th and the 20th, and one-off charges at the same price between them
    bills = [_t(date(2026, m, d), -2500) for m in range(1, 7) for d in (5, 20)]
    days = [(1, 12), (1, 28), (2, 13), (3, 27), (4, 11), (5, 29)]
    one_offs = [_t(date(2026, m, d), -2500) for m, d in days]
    group = sorted([*bills, *one_offs], key=lambda t: t.posted_on)
    assert _ids_of(concurrent_tracks(group)) == _ids_of([bills[::2], bills[1::2]])
    busier = sorted([*group, _t(date(2026, 6, 13), -2500)], key=lambda t: t.posted_on)
    assert concurrent_tracks(busier) is None


def test_a_track_whose_price_wanders_is_no_bill() -> None:
    # same day each month, but one track's charges sit more than 20% from their median
    steady = _monthly(3, range(1, 6), -1500)
    wandering = [
        _t(date(2026, m, 18), c)
        for m, c in zip(range(1, 6), (-1100, -1300, -1500, -1750, -1750), strict=True)
    ]
    assert concurrent_tracks(sorted([*steady, *wandering], key=lambda t: t.posted_on)) is None


def test_subscription_with_orders_splits_off_the_subscription() -> None:
    sub = _monthly(15, range(3, 7), -999)
    orders = [
        _t(date(2026, 3, 3), -2350),
        _t(date(2026, 4, 9), -1720),
        _t(date(2026, 5, 20), -3100),
        _t(date(2026, 6, 2), -2640),
    ]
    bills = split_bills(sorted(sub + orders, key=lambda t: t.posted_on))
    assert bills is not None
    assert [[t.id for t in b] for b in bills] == [[t.id for t in sub]]


def test_variable_bill_is_not_split() -> None:
    group = [
        _t(date(2026, 3, 10), -8000),
        _t(date(2026, 4, 10), -17000),
        _t(date(2026, 5, 10), -9500),
        _t(date(2026, 6, 10), -12000),
    ]
    assert split_bills(group) is None


def test_clean_subscription_is_not_split() -> None:
    assert split_bills(_monthly(15, range(3, 7), -999)) is None


def test_payer_streams_at_different_amounts_split() -> None:
    start = date(2026, 5, 1)
    a = [_t(start + timedelta(days=14 * i), 150000) for i in range(5)]
    b = [_t(start + timedelta(days=7 + 14 * i), 200000) for i in range(5)]
    bills = split_bills(sorted(a + b, key=lambda t: t.posted_on))
    assert bills is not None
    assert sorted(len(b) for b in bills) == [5, 5]


def test_tiny_adjustment_does_not_split_the_bill() -> None:
    # a sub-scale charge is an adjustment, not a second bill: the group stays whole and
    # detection's minor-charge filter drops it
    group = [
        _t(date(2026, 4, 27), -26665),
        _t(date(2026, 5, 12), -189),
        _t(date(2026, 5, 24), -22522),
        _t(date(2026, 6, 27), -25059),
    ]
    assert split_bills(group) is None


def test_variable_bill_with_a_small_fee_is_not_split() -> None:
    # a utility's high months form their own scheduled band; a small fee inside that
    # span must not count as an extra and split off a fixed series
    group = [
        _t(date(2026, 1, 10), -8000),
        _t(date(2026, 2, 10), -8200),
        _t(date(2026, 3, 10), -15000),
        _t(date(2026, 4, 10), -15500),
        _t(date(2026, 4, 18), -1500),
        _t(date(2026, 5, 10), -16000),
        _t(date(2026, 6, 10), -9000),
    ]
    assert split_bills(group) is None


_SMALL_PURCHASES = [
    (date(2026, 1, 17), -299),
    (date(2026, 1, 20), -499),
    (date(2026, 2, 14), -349),
    (date(2026, 2, 17), -450),
    (date(2026, 3, 20), -299),
    (date(2026, 3, 22), -399),
    (date(2026, 4, 25), -499),
    (date(2026, 5, 28), -320),
    (date(2026, 6, 1), -380),
]


def test_whole_bill_drops_small_purchases_beside_a_subscription() -> None:
    # nine small purchases drag the group median down to their size, so the group's own
    # median would keep them; the declined subscription unit sets the scale instead
    sub = _monthly(15, range(1, 7), -1999)
    small = [_t(d, c) for d, c in _SMALL_PURCHASES]
    group = sorted(sub + small, key=lambda t: t.posted_on)
    assert split_bills(group) is None
    assert [t.id for t in whole_bill(group)] == [t.id for t in sub]


def test_whole_bill_keeps_a_variable_bill_and_drops_only_the_fee() -> None:
    group = [
        _t(date(2026, 1, 10), -8000),
        _t(date(2026, 2, 10), -8200),
        _t(date(2026, 3, 10), -15000),
        _t(date(2026, 4, 10), -15500),
        fee := _t(date(2026, 4, 18), -1500),
        _t(date(2026, 5, 10), -16000),
        _t(date(2026, 6, 10), -9000),
    ]
    kept = whole_bill(group)
    assert {t.id for t in kept} == {t.id for t in group} - {fee.id}


def test_long_monthly_history_is_not_split_into_annual_tracks() -> None:
    band = (
        _monthly(11, range(1, 13), -5000, year=2023)
        + _monthly(11, range(1, 13), -5000, year=2024)
        + _monthly(11, range(1, 13), -5000, year=2025)
    )
    assert concurrent_tracks(band) is None
    assert split_bills(band) is None


def test_concurrent_annual_subscriptions_become_tracks() -> None:
    a = [_t(date(y, 3, 1), -9999) for y in (2023, 2024, 2025)]
    b = [_t(date(y, 9, 1), -9999) for y in (2023, 2024, 2025)]
    band = sorted(a + b, key=lambda t: t.posted_on)
    tracks = concurrent_tracks(band)
    assert tracks is not None
    assert sorted(len(tr) for tr in tracks) == [3, 3]


def test_same_day_collision_follows_expectation() -> None:
    # two $20 subscriptions bill on the 11th and the 13th; in April both post on the 13th,
    # and the early one's charge belongs to the track expecting the 11th
    early = _monthly(11, range(1, 7), -2000)
    early[3] = _t(date(2026, 4, 13), -2000)
    late = _monthly(13, range(1, 7), -2000)
    tracks = concurrent_tracks(sorted(early + late, key=lambda t: (t.posted_on, t.id)))
    assert tracks is not None
    # the two April 13th charges are interchangeable; each track gets one of them
    assert sorted([t.posted_on for t in tr] for tr in tracks) == sorted(
        [[t.posted_on for t in early], [t.posted_on for t in late]]
    )


def test_a_charge_after_the_schedule_before_it_is_due_splits_it() -> None:
    # three installments, then a deposit before the next installment is due
    installments = _monthly(15, range(6, 9), -120000)
    assert split_bills([*installments, _t(date(2026, 9, 1), -210000)]) == [installments]


def test_a_repriced_charge_in_the_due_window_does_not_split() -> None:
    # the next charge at a new price is that charge, not an extra: one bill, as before
    charges = [*_monthly(15, range(5, 8), -1500), _t(date(2026, 8, 12), -2000)]
    assert split_bills(charges) is None


def test_the_not_yet_due_window_closes_on_the_day_the_next_charge_is_nearly_due() -> None:
    # the next installment is nearly due TOLERANCE days before its due date: a charge that
    # day is judged by the run alone, one the day before it still sits beside the schedule
    installments = _monthly(15, range(6, 9), -120000)
    nearly_due = advance_expected_on(date(2026, 8, 15), Cadence.monthly) - timedelta(
        days=TOLERANCE[Cadence.monthly]
    )
    day_before = nearly_due - timedelta(days=1)
    assert split_bills([*installments, _t(day_before, -210000)]) == [installments]
    assert split_bills([*installments, _t(nearly_due, -210000)]) is None


def test_a_later_charge_closes_the_not_yet_due_window() -> None:
    # a purchase just after the newest charge, then the plan goes on at a new price: the
    # next charge came due, so the purchase is judged by the run alone, as before
    charges = [
        *_monthly(9, range(3, 7), -4000),
        _t(date(2026, 6, 12), -30000),
        *_monthly(9, range(7, 10), -8000),
    ]
    assert split_bills(charges) is None


def test_match_across_a_miss_bridges_exactly_one_missed_month() -> None:
    dates = [date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15), date(2026, 10, 15)]
    assert match_across_a_miss(dates) == (Cadence.monthly, date(2026, 6, 15), date(2026, 8, 15))
    # two months in a row, or two separate months, are a pause, not one missed month
    assert match_across_a_miss([date(2026, 6, 15), date(2026, 7, 15), date(2026, 10, 15)]) is None
    assert match_across_a_miss([date(2026, 4, 15), date(2026, 6, 15), date(2026, 8, 15)]) is None


def test_match_across_a_miss_needs_the_periods_to_median_about_a_month() -> None:
    def after(*gaps: int) -> list[date]:
        dates = [date(2026, 1, 1)]
        for gap in gaps:
            dates.append(dates[-1] + timedelta(days=gap))
        return dates

    # every gap passes the loose per-gap test; the periods (gap / 2 across the miss) then
    # median 36 days, exactly TOLERANCE off a month, and 37 days is one day too far
    assert match_across_a_miss(after(36, 36, 72)) == (
        Cadence.monthly,
        date(2026, 1, 1),
        date(2026, 1, 1) + timedelta(days=72),
    )
    assert match_across_a_miss(after(37, 37, 72)) is None


def test_a_one_bill_merchant_with_a_missed_month_stays_one_whole_bill() -> None:
    # nothing else is charged, so the bridged schedule has no extra to split from
    charges = [*_monthly(15, range(6, 9), -120000), _t(date(2026, 10, 15), -120000)]
    assert split_bills(charges) is None
    assert whole_bill(charges) == charges


def test_a_schedule_holds_across_one_missed_month() -> None:
    # September's installment was paid from elsewhere: only the deposit posted that month
    installments = [*_monthly(15, range(6, 9), -120000), _t(date(2026, 10, 15), -120000)]
    assert split_bills([*installments, _t(date(2026, 9, 1), -210000)]) == [installments]


def test_a_charge_on_the_missed_due_date_is_that_month_not_a_miss() -> None:
    # a variable bill whose July charge fell into another amount band: July was billed
    charges = [
        _t(date(2026, 6, 30), -9000),
        _t(date(2026, 7, 31), -6000),
        _t(date(2026, 8, 31), -8500),
        _t(date(2026, 9, 30), -10500),
    ]
    assert split_bills(charges) is None


def test_a_habit_that_repeats_a_price_is_no_schedule_across_a_miss() -> None:
    # the same dish some days or weeks apart, nothing at that price in August
    dish = [(5, 24), (6, 8), (6, 12), (6, 15), (7, 13), (9, 21)]
    charges = [
        *(_t(date(2026, month, day), -4000) for month, day in dish),
        _t(date(2026, 8, 1), -5500),
        _t(date(2026, 8, 23), -10000),
    ]
    assert split_bills(charges) is None


def test_a_habit_busy_across_the_gap_is_no_missed_month() -> None:
    # one price 33 and 52 days apart reads as a schedule with a skipped month, but the
    # merchant kept charging at real size through the gap: a habit, not a skipped installment
    first = date(2026, 3, 1)
    price = [first, first + timedelta(days=33), first + timedelta(days=85)]
    busy = [first + timedelta(days=33 + 9), first + timedelta(days=33 + 17)]
    charges = [
        *(_t(d, -4000) for d in price),
        _t(busy[0], -9000),
        _t(busy[1], -13000),
    ]
    assert match_across_a_miss(price) is not None  # the dates alone would bridge
    assert split_bills(charges) is None


def test_one_real_size_charge_in_the_gap_still_bridges_and_two_do_not() -> None:
    installments = [*_monthly(15, range(6, 9), -120000), _t(date(2026, 10, 15), -120000)]
    deposit = _t(date(2026, 9, 1), -210000)
    assert split_bills([*installments, deposit]) == [installments]
    assert split_bills([*installments, deposit, _t(date(2026, 9, 3), -90000)]) is None


def test_small_charges_in_the_gap_do_not_count_as_the_merchant_being_busy() -> None:
    # fees under a quarter of the installment are adjustments, so the merchant stayed quiet
    installments = [*_monthly(15, range(6, 9), -120000), _t(date(2026, 10, 15), -120000)]
    gap = [
        _t(date(2026, 9, 1), -210000),
        _t(date(2026, 9, 2), -14000),
        _t(date(2026, 9, 4), -20000),
    ]
    assert split_bills([*installments, *gap]) == [installments]
