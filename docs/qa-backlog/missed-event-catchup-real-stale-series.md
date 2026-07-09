# Missed-event catch-up behaves sanely against a real stale recurring series

**Feature:** missed-event catch-up loop (`emit_series_events` in `src/moneta/pipelines/events.py`,
calendar-aware `advance_expected_on` in `src/moneta/pipelines/recurring.py`)
**Priority:** high
**Type:** functional

## Prerequisites
- A real moneta DB that hasn't been synced in a while (weeks to months), so at least one
  active recurring series has fallen multiple cadence periods behind `next_expected_on` —
  or deliberately stop syncing for a while to create this condition, or roll back to an
  older DB snapshot with an active series and a large gap before today.
- `moneta recurring` to identify a candidate active series before triggering the catch-up.

## Test Steps
1. `uv run moneta recurring` — note an active series whose `next_expected_on` is well in
   the past (multiple cadence periods behind today).
2. `uv run moneta sync` (a normal sync, not `--full`) to trigger event emission for the
   stale gap in one pass.
3. `uv run moneta recurring --events` — inspect how many missed/catch-up events were
   emitted for that series in this single run.
4. Confirm `next_expected_on` on the series advanced all the way to the correct future
   date in one pass (not stuck one period behind, requiring another sync to catch up
   further).
5. If the series actually did get paid during the gap (e.g. it's a subscription you still
   have), confirm the specific windows with a matching real transaction were *not*
   double-flagged as missed.
6. Check `moneta power` fixed-cost total isn't skewed by a burst of spurious missed-event
   noise from the catch-up.

## Expected Result
- A single sync correctly catches a series up through every missed period since the last
  run, emitting one event per genuinely missed window and none for windows that were
  actually paid.
- `next_expected_on` lands on the correct future occurrence relative to today, using
  calendar-aware advancement (e.g. a monthly series keeps its day-of-month rather than
  drifting by the old fixed 30-day step).
- No runaway/duplicate event emission on a subsequent sync for the same already-caught-up
  gap.

## Notes
- `tests/test_events.py::test_missed_payments_catch_up_all_periods` and
  `test_catch_up_skips_windows_with_payment` cover the catch-up loop mechanically against
  synthetic multi-month gaps with hand-placed transactions. They can't catch real-world
  messiness: a series whose cadence classification itself becomes borderline after a long
  gap, real transactions posting a day or two off from the expected window (interacting
  with the `_GRACE` tolerance), or a merchant renaming mid-gap that splits what should be
  one series into two. This item is about validating the catch-up behavior against
  whatever genuinely irregular history your real accounts have accumulated, not the clean
  synthetic case.
