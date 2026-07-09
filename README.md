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

The first sync pulls all history your institutions retain. `moneta sync --full`
re-pulls everything — use it after linking a new account so its history isn't
skipped by the incremental window.

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
`MONETA_SIMPLEFIN_ACCESS_URL`, `MONETA_LLM_MODEL` (any LiteLLM model string; unset =
no LLM, ambiguous items go to `moneta review`), `MONETA_API_URL`, `MONETA_DB_PATH`,
`MONETA_CONFIG_DIR` (config-file location; default `~/.config/moneta`).

## Design

See `docs/superpowers/specs/2026-07-07-moneta-design.md`.
