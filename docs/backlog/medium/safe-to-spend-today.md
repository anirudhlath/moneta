# Add "safe to spend per day" to the power view

## Summary
`moneta power` shows monthly remaining; add a derived per-day guidance number:
`remaining ÷ days left in month`, shown as one extra row.

## Context / motivation
"Remaining: $1,800" requires mental math to act on mid-month. "$60/day for the
next 22 days" is directly actionable and matches the app's one-honest-number
thesis. All inputs already exist in `PowerReport` (`remaining`) plus the
request-time `today`.

## Acceptance criteria
- `PowerReport` gains a `per_day_remaining` (or similar) field computed as
  `remaining / days_left_in_month` (inclusive of today; month-end day counts
  as 1 day left — no division by zero).
- Negative remaining shows a negative per-day number (don't clamp to zero —
  honesty over comfort).
- `moneta power` renders it as a row, e.g. "Per day (22 days left)  $60.12".
- Derived only — no new stored state.
