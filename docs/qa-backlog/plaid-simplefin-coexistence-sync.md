# SimpleFIN + Plaid coexistence: one sync merges both sources cleanly

**Feature:** Multi-aggregator sync — `MergedAdapter` (`src/moneta/aggregator/base.py`, wired in `_build_adapter` in `src/moneta/api.py`)
**Priority:** high
**Type:** integration

## Prerequisites
- A working SimpleFIN Bridge connection with real linked accounts (see `simplefin-real-bridge-connection.md`)
- At least one linked Plaid item (sandbox or production) in the same config dir
- Both configured simultaneously: `simplefin_access_url` and `plaid_client_id`/`plaid_secret` present in config

## Test Steps
1. With both sources configured, run `uv run moneta sync` once.
2. Confirm the sync summary reflects accounts/transactions from *both* sources in a single run, and `uv run moneta accounts` lists SimpleFIN institutions and Plaid institutions side by side.
3. If the same real bank is linked through both aggregators, inspect that bank's transactions: rows will be duplicated (SimpleFIN and Plaid IDs never collide by design) — confirm this matches expectations and document the observed behavior; avoid double-linking one bank in normal use.
4. Time the sync loosely — both adapters fetch concurrently, so wall time should be near the slower source, not the sum.
5. Re-run `uv run moneta sync` — idempotent for both sources; counts stable.
6. Failure isolation check: temporarily break the SimpleFIN access URL (edit config to a bad path), sync, and confirm the whole sync fails loudly — no partial Plaid-only ingest. Restore config afterwards.
7. Remove `plaid_items.json` (or corrupt it) with Plaid creds still set: sync must still run SimpleFIN-only, with a warning about the items file in server logs; the corrupt-file case tells you to delete and re-link.

## Expected Result
- One `moneta sync` pulls every configured source through a single merged snapshot; pipelines and views behave identically regardless of which aggregator supplied a row.
- Re-sync introduces no duplicates within either source.
- A source-level (credential) failure fails the whole sync before anything is ingested; a missing/corrupt Plaid items file only drops Plaid from the merge.

## Notes
- `gather_snapshots` is deliberately fail-fast on non-item errors: a dead SimpleFIN URL aborts the merged fetch even though Plaid succeeded (ingest happens only after all fetches settle, so nothing partial lands). Per-item Plaid failures are the exception — see `plaid-item-login-required-degradation.md`.
- Sync windows differ by design: SimpleFIN gets the incremental `since` window while Plaid ignores `since` and replays 730 days every run; `--full` only changes SimpleFIN behavior. Cross-source window unification is tracked in `docs/backlog/medium/per-source-sync-window.md`.
- Transfer detection across sources (e.g. a payment leaving a SimpleFIN checking account into a Plaid credit card) is worth a spot-check in `moneta review`/power once both have real data.
