# Plaid ITEM_LOGIN_REQUIRED: dead item degrades to warn+skip, sync continues

**Feature:** Plaid integration — per-item error degradation (`PlaidAdapter._fetch_item_or_skip` in `src/moneta/aggregator/plaid.py`)
**Priority:** high
**Type:** integration

## Prerequisites
- A linked sandbox Plaid item (see `plaid-sandbox-hosted-link-first-sync.md`) plus at least one other working source (a second sandbox item or SimpleFIN)
- Plaid sandbox keys and the item's `access_token` (read it from `<config_dir>/plaid_items.json`)
- `moneta serve` running in a terminal where its log output is visible, to corroborate the CLI-surfaced warning against the server log

## Test Steps
1. Force the item into re-auth state: `curl -X POST https://sandbox.plaid.com/sandbox/item/reset_login -H 'Content-Type: application/json' -d '{"client_id":"...","secret":"...","access_token":"<from plaid_items.json>"}'`. (Alternative: let a sandbox item age past ~30 days — they expire naturally.)
2. Run `uv run moneta sync` (via the server, `MONETA_API_URL` set, so logs are observable).
3. Check both: the CLI's own sync output prints a yellow `⚠ Plaid item <institution> skipped (ITEM_LOGIN_REQUIRED: ...) — repair with: moneta setup plaid-relink <item-id>` line right in the summary (`SyncReport.warnings`), and the server log carries the same warning.
4. Confirm the sync itself succeeded: exit code 0, `moneta status` shows `last_sync_ok: true`, and accounts/transactions from the *other* source(s) were ingested this run.
5. Verify no partial rows from the dead item: its account balances/dates in `moneta accounts` are unchanged from the previous successful sync (the adapter builds each item's snapshot locally and discards it wholesale on failure).
6. Repair the item with `uv run moneta setup plaid-relink <item-id>` (the id from the warning or `plaid-list`), sync again, and confirm data for it resumes — same item id, same accounts, no duplicates (this is update-mode repair-in-place, not the old unlink+`plaid-link` flow, which mints a *new* item id and would duplicate accounts). Real-API verification of `plaid-relink` itself is tracked separately in `plaid-relink-real-item.md`.

## Expected Result
- One dead bank never blocks the rest of the sync; the failure is surfaced both as a yellow warning line in the `moneta sync` CLI output and in the server log, with an actionable repair hint.
- No partial account/balance updates leak from the failed item.
- After `plaid-relink`, history resumes cleanly with no duplicate accounts.

## Notes
- Only `error_type == "ITEM_ERROR"` degrades; credential-level errors (e.g. `INVALID_API_KEYS`) stay fatal only when Plaid is the *sole* configured source or every configured source fails in the same run — with another healthy source configured (e.g. SimpleFIN), a bad Plaid secret now also degrades to a warning (`⚠ plaid: ...`) rather than failing the whole sync. Optionally verify both: a bad secret with no other source configured fails the sync loudly; the same bad secret alongside a healthy SimpleFIN source degrades to a warning and SimpleFIN's data still ingests.
- The per-item CLI-surfacing gap this ticket used to track is closed: `Snapshot.warnings` (per-item Plaid skip messages, SimpleFIN bridge errors) flow through `SyncReport.warnings` and print as `⚠ {warning}` in the `moneta sync` summary — see the 2026-07-16 "Sync warnings surfaced" entry in `docs/PRD.md`.
- `moneta setup plaid-link` still mints a *new* item_id if used to "re-link" a dead item — don't use it for repair; `moneta setup plaid-relink <item-id>` (hosted-link update mode) is the correct fix and is what the CLI hint now points at.
