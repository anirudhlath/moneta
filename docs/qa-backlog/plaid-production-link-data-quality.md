# Plaid production: real institution link and first-sync data quality

**Feature:** Plaid integration — production data quality (`src/moneta/aggregator/plaid.py`)
**Priority:** critical
**Type:** integration

## Prerequisites
- Plaid production keys with Trial/pay-as-you-go access approved on the dashboard
- A real bank account to link (ideally one checking + one credit card; a loan and an investment account if available, to exercise every mapped `AccountType`)
- moneta with a fresh or backed-up db (`MONETA_DB_PATH` at a scratch path is safest)

## Test Steps
1. `uv run moneta setup plaid <CLIENT_ID> <PROD_SECRET>` (env defaults to production), then `uv run moneta setup plaid-link` and complete Link in the browser against the real institution.
2. `uv run moneta sync` — first sync; Plaid returns up to 730 days of history (`days_requested` is set at link-token creation).
3. `uv run moneta accounts` — verify names, org (institution) name, and mapped types against reality; fix any misses with `--set-type` and confirm the override survives a re-sync.
4. `uv run moneta networth` — credit-card and loan balances must appear negative (amount owed); Plaid reports them positive and the adapter negates. Compare magnitudes against the institution's own site.
5. `uv run moneta power` and `uv run moneta cashflow` — spot-check 5–10 known real transactions: outflows negative, inflows (paychecks, refunds) positive, dates match posted dates, descriptions usable.
6. If an investment account was linked with `--product investments`, check holdings: symbols from ticker (fallback security name), market values sane.
7. Re-run `uv run moneta sync` and confirm counts don't grow (no duplicates from the full-history replay).

## Expected Result
- Every linked account lands with correct type, sign-correct balance, and sign-correct transactions end-to-end into the power/cashflow/networth views.
- Pending transactions are absent (the adapter skips `pending: true`); they appear on a later sync once posted, without duplicating.
- Re-sync is idempotent.

## Notes
- Data quality varies by institution — descriptor quality, `balances.last_updated_datetime` presence (fallback is today), and whether `balances.current` is null (liability fallback is 0, depository fallback is `available`). Only real institutions exercise these fallbacks.
- Depository subtypes other than checking all map to savings (money market, CD, HSA…) — judge whether that's acceptable for the linked accounts or needs `--set-type`.
- Production Link sessions bill per Plaid's pricing; keep the number of link attempts in mind.
