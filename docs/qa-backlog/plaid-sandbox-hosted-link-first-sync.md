# Plaid sandbox: hosted Link flow end-to-end, first sync lands data

**Feature:** Plaid integration — hosted Link + first sync (`src/moneta/aggregator/plaid.py`, `moneta setup plaid*` in `src/moneta/cli/main.py`)
**Priority:** critical
**Type:** e2e

## Prerequisites
- A Plaid account with sandbox keys from https://dashboard.plaid.com (sandbox is free)
- A browser on the same machine (Hosted Link URL is printed by the CLI and opened manually)
- moneta installed locally (`uv sync`), fresh `MONETA_CONFIG_DIR` pointed at a scratch dir

## Test Steps
1. `uv run moneta setup plaid <CLIENT_ID> <SANDBOX_SECRET> --env sandbox` — confirm `[green]Plaid credentials saved.[/green]` and that `config.toml` in the config dir gained `plaid_client_id`, `plaid_secret`, `plaid_env = "sandbox"`.
2. `uv run moneta setup plaid-link` — a Hosted Link URL prints and the CLI blocks on "Waiting for you to finish (Ctrl-C aborts)…".
3. Open the URL in a browser, pick a sandbox institution (e.g. First Platypus Bank), and sign in with `user_good` / `pass_good`.
4. Within a few polling intervals (3 s each) the CLI should print `[green]Linked <institution>.[/green] Run: moneta sync`.
5. Inspect `<config_dir>/plaid_items.json`: one entry with `item_id`, `access_token`, `institution_name`, `products: ["transactions"]`; file mode is `0600` (`ls -l`).
6. `uv run moneta setup plaid-list` — table shows the institution, item id, and products.
7. `uv run moneta sync`, then `uv run moneta accounts` — sandbox accounts appear with types mapped from Plaid type/subtype (depository/checking → checking, other depository → savings, credit → credit, loan → loan, investment → brokerage), not the name-keyword fallback.
8. `uv run moneta cashflow` (or query the db) — spot-check sandbox transactions: outflows negative, inflows positive; credit/loan balances show negative (owed) in `moneta networth`.
9. Re-run `uv run moneta sync` — no duplicate accounts or transactions (Plaid replays full history every run; ingest dedup must absorb it).

## Expected Result
- Link completes browser-side with zero manual token handling; the CLI polls to completion and persists the item atomically with 0600 permissions.
- First sync ingests sandbox accounts and transactions with moneta's sign convention (Plaid's amounts are inverted by the adapter).
- Re-sync is idempotent despite the deliberate full-history replay.

## Notes
- Right after linking, `/transactions/sync` may report `transactions_update_status: NOT_READY` — the adapter logs "history still preparing" and returns few/no transactions; run `moneta sync` again a minute later.
- Test Ctrl-C during step 2's wait and the 900 s poll timeout message ("re-run: moneta setup plaid-link") if convenient.
- `plaid-link` links one institution per run; repeat it to link a second sandbox bank and confirm `plaid_items.json` accumulates entries.
- Design spec: `docs/superpowers/specs/2026-07-09-plaid-integration-design.md` §4–5.
