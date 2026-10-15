from datetime import date, timedelta

from moneta.cadence import match_across_a_miss
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


def test_weekly_band_is_never_split_into_tracks() -> None:
    band = [_t(date(2026, 1, 5) + timedelta(days=7 * i), -500) for i in range(12)]
    assert concurrent_tracks(band) is None


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
