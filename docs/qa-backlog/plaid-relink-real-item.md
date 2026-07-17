# Plaid relink: real ITEM_LOGIN_REQUIRED item repaired via hosted-link update mode

**Feature:** `moneta setup plaid-relink <item-id>` (`src/moneta/cli/main.py::setup_plaid_relink`, `aggregator/plaid.py::create_hosted_link(..., access_token=...)`)
**Priority:** high
**Type:** e2e

## Prerequisites
- A real (non-sandbox, or sandbox — either works) linked Plaid item that has actually entered `ITEM_LOGIN_REQUIRED` (see `plaid-item-login-required-degradation.md` for how to force this in sandbox; for production, wait for a real credential rotation/MFA change to break a linked bank).
- The item's real accounts already synced at least once, with transaction history in the database, so a duplicate-vs-same-account regression would be visible.
- `<config_dir>/plaid_items.json` readable, to record the item's `item_id`/`access_token` before and after the test.

## Test Steps
1. Capture the "before" state: `uv run moneta setup plaid-list` (note the item id and institution), the raw `item_id`/`access_token` pair from `plaid_items.json`, and `uv run moneta accounts` (account ids/names/balances for that institution).
2. Confirm the item is actually broken: `uv run moneta sync` should show the `⚠ Plaid item <institution> skipped (ITEM_LOGIN_REQUIRED: ...) — repair with: moneta setup plaid-relink <item-id>` warning.
3. Run `uv run moneta setup plaid-relink <item-id>` using the id from step 1/2. Confirm it prints a hosted-link URL and waits.
4. Open the URL in a browser and complete Plaid's real re-authentication flow for that institution (this is the part that can't be automated/mocked — real Plaid Link UI, real bank MFA).
5. After the CLI reports success (`Relinked <institution>.`), inspect `plaid_items.json` again: confirm the **same** `item_id` is present, and note whether `access_token` changed (Plaid may or may not rotate it in update mode — either is acceptable, but the `item_id` and the row count must be unchanged, i.e. no new item appended).
6. Run `uv run moneta sync` again. Confirm: no new/duplicate accounts appear in `uv run moneta accounts` for that institution (compare against the step 1 snapshot), the previously-broken item now syncs cleanly (no `ITEM_LOGIN_REQUIRED` warning), and existing transaction history / recurring series for those accounts are untouched (same account ids, so series/links naturally carry over).

## Expected Result
- The hosted-link update-mode flow completes in the browser against Plaid's real API (not mockable — `PlaidClient.post` really talks to `/link/token/create` with `access_token` set, `/link/token/get` for polling).
- After relinking, `plaid_items.json` still has exactly one entry for that institution (same `item_id`) — no duplicate item, no duplicate accounts, no exchange call (there's no `public_token`/`exchange_public_token` in this flow, per the design).
- The next `moneta sync` succeeds for that institution with no `ITEM_LOGIN_REQUIRED` warning and no data discontinuity (existing transactions/series keep their account linkage).

## Notes
- Everything except the real browser re-auth step is covered by unit/CLI tests with mocked `plaid` module functions (`tests/test_cli.py::test_setup_plaid_relink_keeps_item_unchanged`, `tests/test_plaid.py::test_create_hosted_link_includes_access_token_when_passed`) — this QA item exists specifically because Plaid's actual update-mode re-auth UX (does it always keep the same `item_id`? does `access_token` rotate? does Plaid ever silently create new `account_id`s for the same real account during update mode?) cannot be verified without hitting Plaid's real API and a real bank's MFA flow.
- If Plaid *does* rotate `account_id`s during update mode for some institution (this varies by integration and isn't something moneta controls), that would silently orphan existing transactions/series from the pre-relink accounts — worth explicitly checking for and filing a follow-up ticket if observed, since moneta's data model assumes an account's `aggregator_id` is stable.
