# Cashflow accrual vs cash-out sanity check on real data

**Feature:** `moneta cashflow [--start/--end]` CLI command (`src/moneta/cli/main.py`, `src/moneta/views/cashflow.py`)
**Priority:** high
**Type:** e2e

## Prerequisites
- At least one full real month of synced SimpleFIN data including credit-card purchases, a credit-card payment from checking, and at least one internal transfer (e.g. checking → savings).
- Transfer dedup verified first (`transfer-dedup-accuracy-real-data.md`) — both cashflow numbers exclude transactions via `classified_links`, so a bad link poisons them.

## Test Steps
1. `uv run moneta sync`, then `uv run moneta cashflow` (no flags) — confirm the title shows the current month start through today.
2. Manually reconcile "Accrual spend" for the range: it should equal the sum of purchases on spend accounts (checking + credit), with transfer legs excluded — spot-check against bank/card statements.
3. Manually reconcile "Cash out" for the same range: money that actually left liquid accounts (checking/savings), so the CC *payment* counts here (not the individual card purchases), while checking → savings moves count in neither number.
4. Verify the accrual/cash-out distinction on a real credit-card cycle: a month with heavy card use but no payment yet should show accrual > cash out; the month the payment posts should show the reverse pressure.
5. Run `uv run moneta cashflow --start <first-of-last-month> --end <last-of-last-month>` for a completed month and reconcile both numbers against that month's statements.
6. Cross-check consistency: for the default range, "Accrual spend" should be in the same ballpark as `moneta power`'s "Spent so far" (both are accrual-over-the-month with linked txns excluded; power additionally excludes recurring fixed-cost occurrences, so accrual ≥ spent-so-far is not guaranteed — but a wild divergence needs explaining).
7. Try a range with zero transactions (e.g. `--start 2019-01-01 --end 2019-01-31`) and confirm both numbers are $0, not an error.

## Expected Result
- Accrual spend matches real purchases for the range; cash out matches real money leaving liquid accounts; internal liquid→liquid moves appear in neither.
- Credit-card purchases are counted once in accrual (when made) and once in cash out (when the payment posts) — never doubled within one number.
- Explicit `--start/--end` ranges return the same numbers a manual statement reconciliation gives for that window.

## Notes
- The `/cashflow` API endpoint predates the CLI command; the automated tests cover flag parsing, param passing, and empty-DB rendering, but no automated test can validate the numbers against real bank statements.
- Boundary semantics are inclusive on both ends (`posted_on >= start`, `<= end`) — when reconciling a month, use the last day of the month as `--end`, not the first of the next.
- Accrual scans `SPEND_ACCOUNT_TYPES`, cash out scans `LIQUID_ACCOUNT_TYPES` — a real account with a wrongly inferred type (see `moneta accounts --set-type`) will silently shift spend between the two numbers; if a number looks off, check account types first.
