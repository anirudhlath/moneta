# Spending-power number sanity check for one full real month

**Feature:** Spending power flagship view (`uv run moneta power`, `src/moneta/views/power.py`)
**Priority:** critical
**Type:** e2e

## Prerequisites
- A full real month of synced data across all accounts.
- A side-by-side number from the user's prior app (Origin or Copilot), or a manual budget/mental model, for the same month.

## Test Steps
1. `uv run moneta sync` at the start of a real month, and again at month's end (or mid-month, tracked over time).
2. `uv run moneta power` — record Income, Fixed costs (with the itemized list), Spending power, Spent so far, Remaining.
3. Manually reconcile: does "Income (detected)" match actual real paycheck deposits for the month? Does each line in "Fixed costs" correspond to a real recurring bill, with no phantom entries and none missing?
4. Compare "Spent so far" against the same period in Origin/Copilot (or a manual ledger) — do they roughly agree once transfers are correctly excluded (design doc §7.2 accrual definition)?
5. Specifically verify credit-card purchases are counted once — as spend when made, not double-counted again when the CC payment posts (design doc §7.2 accrual vs. cash-out distinction).
6. At month end, confirm "Remaining" reaches (or comes close to) zero/matches actual leftover cash, and the number "feels right" against gut sense of the month's spending.

## Expected Result
Spending power is a number the user actually trusts enough to make purchase decisions on. Any material discrepancy from the prior app should be explainable (e.g. transfers correctly excluded here but not there), not a silent bug.

## Notes
- This is the flagship number the entire product exists to produce (design doc §1, "The headline goal: answer *monthly spending power = income − fixed costs*, accurately and automatically") — critical by definition.
- Depends transitively on transfer-dedup accuracy and recurring-detection quality being correct first — run `transfer-dedup-accuracy-real-data.md` and `recurring-detection-quality-real-data.md` before or alongside this pass; a bug in either will surface here as a wrong "Spending power" number.
- SDD ledger Task 10 notes are performance-only (`power_report` scans `TransferLink`/`Transaction` twice; "no empty-DB power_report test") — not correctness concerns, but worth knowing if `moneta power` feels slow on a large real DB.
