# Plaid ITEM_LOGIN_REQUIRED: dead item degrades to warn+skip, sync continues

**Feature:** Plaid integration — per-item error degradation (`PlaidAdapter._fetch_item_or_skip` in `src/moneta/aggregator/plaid.py`)
**Priority:** high
**Type:** integration

## Prerequisites
- A linked sandbox Plaid item (see `plaid-sandbox-hosted-link-first-sync.md`) plus at least one other working source (a second sandbox item or SimpleFIN)
- Plaid sandbox keys and the item's `access_token` (read it from `<config_dir>/plaid_items.json`)
- `moneta serve` running in a terminal where its log output is visible (item-level warnings only go to server/CLI logs today)

## Test Steps
1. Force the item into re-auth state: `curl -X POST https://sandbox.plaid.com/sandbox/item/reset_login -H 'Content-Type: application/json' -d '{"client_id":"...","secret":"...","access_token":"<from plaid_items.json>"}'`. (Alternative: let a sandbox item age past ~30 days — they expire naturally.)
2. Run `uv run moneta sync` (via the server, `MONETA_API_URL` set, so logs are observable).
3. Check the server log: a warning naming the institution, the `ITEM_LOGIN_REQUIRED` error, and the hint "re-link with: moneta setup plaid-link".
4. Confirm the sync itself succeeded: exit code 0, and accounts/transactions from the *other* source(s) were ingested this run.
5. Verify no partial rows from the dead item: its account balances/dates in `moneta accounts` are unchanged from the previous successful sync (the adapter builds each item's snapshot locally and discards it wholesale on failure).
6. Re-link the institution with `uv run moneta setup plaid-link`, sync again, and confirm data for it resumes without duplicates (the old dead entry can then be removed with `plaid-unlink`).

## Expected Result
- One dead bank never blocks the rest of the sync; the failure is a log warning with an actionable re-link hint.
- No partial account/balance updates leak from the failed item.
- After re-linking, history resumes cleanly.

## Notes
- Only `error_type == "ITEM_ERROR"` degrades; credential-level errors (e.g. `INVALID_API_KEYS`) stay fatal by design (spec §6) — optionally verify a bad secret fails the whole sync loudly.
- Nothing surfaces in the CLI sync summary today — that gap is tracked in `docs/backlog/medium/surface-per-item-sync-warnings.md`; test against logs, not CLI output.
- Re-linking creates a *new* item_id; the old entry stays in `plaid_items.json` (and keeps warning every sync) until unlinked. Link update mode (repairing in place) is a non-goal, tracked in `docs/backlog/low/plaid-link-update-mode.md`.
