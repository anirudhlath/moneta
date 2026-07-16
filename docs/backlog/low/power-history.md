# Month-over-month power history (`moneta power --history N`)

## Summary
`moneta power` is current-month only. Add `--history N` showing spending
power and actual spend for each of the last N months, so the user can see
whether things are improving.

## Context / motivation
All transaction data is already stored; a history view is a loop over month
windows using the same exclusion rules (`views/power.py` / `views/cashflow.py`).
The one design wrinkle: fixed costs and income are derived from *current*
series state, so historical months should report actual observed
inflow/outflow rather than pretending today's series existed then — decide
and document the semantics (recommend: actual accrual spend + actual income
per past month, current-month logic unchanged).

## Acceptance criteria
- `GET /power/history?months=N` (or params on `/power`) returns per-month
  rows: month, income, spend, net.
- `moneta power --history 6` renders a compact table (optionally a
  sparkline-style bar per month).
- Semantics of past-month income/fixed documented in the endpoint docstring
  and covered by a test with multi-month fixture data (date-relative, per the
  e2e anchoring convention).
- Read-only; no schema changes.
