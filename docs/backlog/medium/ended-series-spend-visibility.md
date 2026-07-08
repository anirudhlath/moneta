# Give ended-but-still-charging series a spend bucket

## Summary
Transactions matching an ENDED recurring series are excluded from both
fixed costs *and* spent-so-far, so a subscription the user cancelled in
`moneta` but that keeps actually charging becomes invisible spend —
`moneta power` overstates spending power by the full amount.

## Context
`detect_recurring` (`src/moneta/pipelines/recurring.py`) matches new
transactions to an existing `RecurringSeries` by `(merchant, direction)`
regardless of `status` — the `existing` lookup is built from *all* series,
not just active ones. So once a series is ended via
`PATCH /recurring/{id}` (`status=ended`), any new matching transaction
still gets `t.series_id = series.id` set on it.

`views/power.py::power_report` then drops it from both places:
- `fixed_costs` only includes series with `status == SeriesStatus.active`
  (line ~44), so the ended series contributes nothing there.
- The `spent_so_far` query filters `Transaction.series_id.is_(None)`
  (line ~80), so a transaction with a (even ended) `series_id` is excluded
  from discretionary spend too.

The money simply disappears from the power report — the charge is real,
still leaving the account, but appears nowhere.

## Acceptance criteria
- Decide the correct bucket for txns whose `series_id` points at an ended
  series: most likely they should count as `spent_so_far` (discretionary,
  since the user no longer expects/budgets for them as a fixed cost).
- `views/power.py` (and any other view reading `series_id`) updated so
  ended-series transactions are counted somewhere, not silently dropped.
- Regression test: end a series, sync a new transaction still matching it,
  assert it appears in `spent_so_far` (or the chosen bucket) and that
  `spending_power`/`remaining` reflect it.
