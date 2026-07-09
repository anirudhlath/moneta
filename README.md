# moneta

Personal finance with one honest number: **monthly spending power = income − fixed costs**.

Replaces Origin/Copilot for: net worth that ignores unvested RSUs, reliable
subscription detection, deduped inter-account transfers, and financing shown as
its real monthly cash hit (not a lump sum).

## Quickstart

```bash
uv sync
uv run moneta setup simplefin <SETUP_TOKEN>   # get one at https://beta-bridge.simplefin.org
uv run moneta sync
uv run moneta power
```

Prefer Plaid (or need an institution SimpleFIN lacks)? Both can be configured at
once — one `moneta sync` pulls from every configured source:

```bash
uv run moneta setup plaid <CLIENT_ID> <SECRET>   # keys from https://dashboard.plaid.com
uv run moneta setup plaid-link                   # prints a URL; finish linking in the browser
uv run moneta sync
```

`plaid-link` links one institution per run (re-run it for each bank);
`moneta setup plaid-list` / `plaid-unlink <item-id>` manage linked banks.

The first sync pulls all history your institutions retain. `moneta sync --full`
re-pulls everything — use it after linking a new SimpleFIN account so its history
isn't skipped by the incremental window. Plaid replays its full history (up to
730 days) on every sync, so plain `moneta sync` suffices after `plaid-link`.

No server needed — the CLI runs the app in-process. To run a server instead:
`uv run moneta serve`, then `export MONETA_API_URL=http://127.0.0.1:8300`.

## Commands

| Command | What it shows |
|---|---|
| `moneta power` | Income, fixed costs, spending power, spent so far, remaining |
| `moneta networth` | Net worth (vested only); unvested listed as potential |
| `moneta recurring [--events]` | Subscriptions/bills; missed payments and price increases |
| `moneta obligations` | Loans/financing: payment, months left, deferred-interest warnings |
| `moneta review` | Resolve ambiguous classifications |
| `moneta accounts` | Accounts; `--set-type ID TYPE`, `--set-promo ID DATE` |
| `moneta import vesting f.csv` | Vesting data (`symbol,vested_quantity,unvested_quantity`) |

## Configuration

Env vars (`MONETA_*`) override `~/.config/moneta/config.toml`:
`MONETA_SIMPLEFIN_ACCESS_URL`, `MONETA_PLAID_CLIENT_ID`, `MONETA_PLAID_SECRET`,
`MONETA_PLAID_ENV` (`production` (default) or `sandbox`), `MONETA_LLM_MODEL` (any
LiteLLM model string; unset = no LLM, ambiguous items go to `moneta review`),
`MONETA_API_URL`, `MONETA_DB_PATH`, `MONETA_CONFIG_DIR` (config-file location;
default `~/.config/moneta`). Linked Plaid banks (item ids + access tokens) live in
`~/.config/moneta/plaid_items.json`.

## Design

See `docs/superpowers/specs/2026-07-07-moneta-design.md`.
