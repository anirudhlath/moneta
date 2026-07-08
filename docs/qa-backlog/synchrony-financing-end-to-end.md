# Synchrony 0% financing end-to-end

**Feature:** Financing/obligations derivation (`src/moneta/views/financing.py`) against a real Synchrony account
**Priority:** critical
**Type:** e2e

## Prerequisites
- A real Synchrony (or similar 0%-promo financed) account linked at SimpleFIN Bridge and synced via moneta, with at least 3 observed payments so recurring detection can find its payment series.

## Test Steps
1. `uv run moneta sync` across enough billing cycles for the payment to repeat ≥3 times (`_MIN_OCCURRENCES` in `recurring.py`). Verify with `uv run moneta accounts` that the account was classified as `loan` (keyword match on `synchrony`/`financing`/`loan` in `pipelines/ingest.py::_TYPE_HINTS`); fix with `uv run moneta accounts --set-type ID loan` if the heuristic missed it.
2. `uv run moneta obligations` — confirm the account shows a non-null `monthly_payment`, `months_left` (= balance ÷ payment, rounded up), and a `payoff_estimate` date.
3. `uv run moneta accounts --set-promo <ID> <YYYY-MM-DD>` — set the real 0%-promo expiration date from the Synchrony statement.
4. `uv run moneta obligations` again — if `payoff_estimate` lands after the promo date, confirm the red `!` deferred-interest warning renders both on the account's row and in the summary line ("payoff lands after the promo expires — deferred interest risk").
5. `uv run moneta power` — confirm the loan's monthly *payment* (not the full remaining balance) is counted as a fixed cost, per design doc §6.4/§7.1.

## Expected Result
The obligation is derived entirely from observed payments (no manual plan entry beyond the optional promo date); months-left/payoff math matches the real statement; the deferred-interest warning fires only when the estimated payoff lands after the promo-end date.

## Notes
- Design doc §1 pain point 4 — "No visibility into actual monthly cash outflow... a Synchrony 0% financed purchase shows as a lump-sum liability instead of the real monthly liquidity hit" — this is called out as a core product motivation, hence critical.
- SDD ledger residuals to check against real data (Task 9): "newest-series selection lacks id tiebreaker for same-day outflows" and the `inflow_txn` variable in `financing.py` "selects all txns on loan account (name misleading; join restricts correctly)" — worth confirming with a loan account that has more than one payment series.
- `months_left = ceil(balance / payment)` and `payoff = today + 30*months_left days` are both approximations — sanity-check against the real payoff date on the Synchrony statement.
