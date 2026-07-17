# SimpleFIN + Plaid coexistence: one sync pulls both sources cleanly

**Feature:** Multi-adapter sync — `run_sync` iterates every configured `AggregatorAdapter`
(`src/moneta/pipelines/run.py`), built by `_build_adapters` in `src/moneta/api.py`. There is
no merging layer (`MergedAdapter` was dissolved 2026-07-16 in favor of per-source sync
windows — each adapter now gets its own incremental `since`, computed from its own accounts'
newest transaction, `Account.source`/migration `0004`); `run_sync` fetches each adapter and
concatenates their snapshots into one before a single `ingest_snapshot` call.
**Priority:** high
**Type:** integration

## Prerequisites
- A working SimpleFIN Bridge connection with real linked accounts (see `simplefin-real-bridge-connection.md`)
- At least one linked Plaid item (sandbox or production) in the same config dir
- Both configured simultaneously: `simplefin_access_url` and `plaid_client_id`/`plaid_secret` present in config

## Test Steps
1. With both sources configured, run `uv run moneta sync` once.
2. Confirm the sync summary reflects accounts/transactions from *both* sources in a single run, and `uv run moneta accounts` lists SimpleFIN institutions and Plaid institutions side by side. `uv run moneta status --json | jq .aggregators` should list both `"simplefin"` and `"plaid"`.
3. If the same real bank is linked through both aggregators, inspect that bank's transactions: rows will be duplicated (SimpleFIN and Plaid IDs never collide by design) — confirm this matches expectations and document the observed behavior; avoid double-linking one bank in normal use.
4. Time the sync loosely — each adapter fetches sequentially in `run_sync` (a deliberate simplicity choice for a single-user nightly sync; each source's `since` is an independent, cheap local DB read, so fetches could be gathered concurrently later if `--full` latency ever becomes a problem); wall time should scale with the sum of both sources' fetch times, not just the slower one.
5. Re-run `uv run moneta sync` — idempotent for both sources; counts stable. Confirm each source's window stays independent: touch only one institution's data upstream (or just note the per-source `since` values via `moneta.log`'s `SimpleFIN: fetching …` INFO lines) and confirm the *other* source isn't re-pulled from further back than its own overlap.
6. Failure isolation check: temporarily break the SimpleFIN access URL (edit config to a bad path), sync, and confirm the sync **succeeds** with a yellow `⚠ simplefin: ...` warning line in the CLI output and Plaid's data still ingests (`moneta status` shows `last_sync_ok: true`) — this is a deliberate change from the old MergedAdapter behavior, which failed the whole sync on any source error. Restore config afterwards, then additionally verify the *all-sources-fail* case: break both SimpleFIN and Plaid credentials and confirm sync now fails loudly (`last_sync_ok: false`, non-zero exit) — a sync must never report success over an empty pull.
7. Remove `plaid_items.json` (or corrupt it) with Plaid creds still set: sync must still run SimpleFIN-only, with a warning about the items file in server logs; the corrupt-file case tells you to delete and re-link.

## Expected Result
- One `moneta sync` pulls every configured source, each on its own incremental window, and merges their snapshots before a single ingest; pipelines and views behave identically regardless of which aggregator supplied a row.
- Re-sync introduces no duplicates within either source, and one source's window is never widened or narrowed by the other's activity (the bug per-source windows fixed: Plaid's daily replay used to pin a shared global window near today, masking a stalled SimpleFIN source).
- A single source's fetch failure degrades to a warning and the sync still succeeds, ingesting whatever the healthy source(s) returned — unless every configured source fails, in which case the sync fails as a whole (nothing partial is ingested, so `data_as_of`/staleness never lies about having fresh data). A missing/corrupt Plaid items file only drops Plaid from this run (same "warn and continue" path).

## Notes
- Per-item Plaid failures (a single bank needing re-link) are a separate, lower-level degrade-and-skip path inside `PlaidAdapter` itself — see `plaid-item-login-required-degradation.md`. This ticket's step 6 is about a whole-adapter failure (e.g. bad SimpleFIN credentials), one level up.
- Sync windows are now genuinely independent per source (no longer "differ by design" as a known gap): SimpleFIN gets its own incremental `since` derived from SimpleFIN-sourced transactions; Plaid still ignores `since` and replays ≤730 days every run regardless. `--full` forces every source to the epoch.
- Transfer detection across sources (e.g. a payment leaving a SimpleFIN checking account into a Plaid credit card) is worth a spot-check in `moneta review`/power once both have real data.
