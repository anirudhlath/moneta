# Synchrony 0% financing end-to-end

**Feature:** Financing/obligations derivation (`src/moneta/views/financing.py`) against a real Synchrony account
**Priority:** critical
**Type:** e2e

## Prerequisites
- A real Synchrony (or similar 0%-promo financed) account linked at SimpleFIN Bridge and synced via moneta, with at least 2 observed payments (per-account payments derive from transfer links; cadence falls back to monthly below 3).

## Test Steps
1. `uv run moneta sync` across enough billing cycles for the payment to repeat. Real Synchrony cards type as `credit` (org keyword order) — the financing fingerprint should open a one-time review question; answer `y` in `uv run moneta review` (or force with `uv run moneta accounts --set-financing ID true`). Verify `uv run moneta accounts` shows `credit (financing)`.
2. `uv run moneta obligations` — confirm the account shows a non-null `monthly_payment_cents`, `months_left` (= balance ÷ payment, rounded up), and a `payoff_estimate` date, with the PER-ACCOUNT payment amount (not a median across sibling Synchrony cards sharing a bank descriptor).
3. `uv run moneta accounts --set-promo <ID> <YYYY-MM-DD>` — set the real 0%-promo expiration date from the Synchrony statement.
4. `uv run moneta obligations` again — if `payoff_estimate` lands after the promo date, confirm the red `!` deferred-interest warning renders both on the account's row and in the summary line ("payoff lands after the promo expires — deferred interest risk").
5. `uv run moneta power` — confirm the account's monthly *payment* appears as a derived fixed-cost line (`<name> — payment`), not the full remaining balance, per design doc §6.4/§7.1.

## Expected Result
The obligation is derived entirely from observed transfer-linked payments (no manual plan entry beyond the optional promo date); per-account amounts are correct even when several cards share one bank descriptor; months-left/payoff math matches the real statement; the deferred-interest warning fires only when the estimated payoff lands after the promo-end date.

## Notes
- Design doc §1 pain point 4 — "No visibility into actual monthly cash outflow... a Synchrony 0% financed purchase shows as a lump-sum liability instead of the real monthly liquidity hit" — this is called out as a core product motivation, hence critical.
- Updated 2026-07-16 for the detection-correctness wave: payments now derive per-account from transfer links (`queries.loan_payment_stats`); the old keyword-based `loan` typing expectation and `_payment_series_id` mechanics no longer apply. See also `financing-fingerprint-real-synchrony-accounts.md` and `loan-payment-self-heal-real-db-migration.md` for the fingerprint- and migration-specific checks.
- `months_left = ceil(balance / payment)` and `payoff = today + 30*months_left days` are both approximations — sanity-check against the real payoff date on the Synchrony statement.
