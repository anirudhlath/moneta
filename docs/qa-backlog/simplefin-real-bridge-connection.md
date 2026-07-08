# Real SimpleFIN Bridge connection: claim, first sync, verify data lands

**Feature:** SimpleFIN Bridge integration (`src/moneta/aggregator/simplefin.py`)
**Priority:** critical
**Type:** integration

## Prerequisites
- A SimpleFIN Bridge account and a one-time setup token from https://beta-bridge.simplefin.org
- Real accounts linked at the bridge (checking, credit card, and ideally a loan/financing and a brokerage account, to exercise every `AccountType`)
- moneta installed locally (`uv sync`), fresh `~/.config/moneta` or `MONETA_CONFIG_DIR` pointed at a scratch dir

## Test Steps
1. `uv run moneta setup simplefin <SETUP_TOKEN>` — claim the one-time token.
2. Confirm `[green]SimpleFIN connected.[/green]` prints and `~/.config/moneta/config.toml` now has a `simplefin_access_url` line.
3. `uv run moneta sync` — first sync; SimpleFIN typically returns 90+ days of history.
4. `uv run moneta accounts` — verify every real linked account appears with the correct name/org, and inspect the inferred `type` column against `pipelines/ingest.py`'s keyword heuristic (`_TYPE_HINTS`: checking/savings/credit/loan/brokerage). Fix any wrong or `unknown` classification with `uv run moneta accounts --set-type ID TYPE`.
5. `uv run moneta power`, `moneta recurring`, `moneta obligations` — spot-check several known real transactions for correct amount sign (negative = outflow) and date.
6. Re-run `uv run moneta sync` a second time and confirm no duplicate transactions or accounts appear (idempotent ingest).

## Expected Result
- Accounts, transactions, and holdings from every linked institution land correctly in the local DB.
- Sign convention is correct end-to-end.
- Re-sync does not duplicate rows.

## Notes
- Design doc §10 risk 2: "SimpleFIN data quality varies by institution" — descriptor quality, latency, and balance freshness differ; this can only be judged against real data.
- `simplefin.py`'s `fetch()` only `logger.warning`s the bridge's `errors` array (e.g. an institution needing re-auth) — nothing surfaces to the CLI today, so check server/CLI logs for partial sync failures, not just the summary line.
- See `simplefin-percent-encoded-credentials.md` for a related known-bug caveat in the auth path used here.
