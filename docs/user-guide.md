# Moneta User Guide

Moneta answers one question from your real bank data: **how much can I actually spend this month?**
(monthly spending power = detected income − detected fixed costs). Everything is read-only —
moneta never moves money.

This guide covers day-to-day usage. For what the product is and why, see the [PRD](PRD.md).

## Contents

- [Install](#install)
- [Quickstart](#quickstart)
- [Connecting data sources](#connecting-data-sources)
- [Syncing](#syncing)
- [Reading the numbers](#reading-the-numbers)
- [The review queue](#the-review-queue)
- [LLM assist (optional)](#llm-assist-optional)
- [RSU vesting import](#rsu-vesting-import)
- [Server mode & remote access](#server-mode--remote-access)
- [Backup](#backup)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:anirudhlath/moneta.git
cd moneta
uv sync
```

Every command below is run as `uv run moneta …` from the repo (or just `moneta …` if you've
activated the venv). No server or daemon is needed — the CLI runs the app in-process.

## Quickstart

```bash
uv run moneta setup simplefin <SETUP_TOKEN>   # get a token at https://beta-bridge.simplefin.org
uv run moneta sync                            # first sync pulls all available history
uv run moneta power                           # the number
```

## Connecting data sources

You can connect SimpleFIN, Plaid, or both — one `moneta sync` pulls from every configured source.

### SimpleFIN

1. Create an account at [beta-bridge.simplefin.org](https://beta-bridge.simplefin.org) and link your banks there.
2. Generate a setup token and claim it once:

```bash
moneta setup simplefin <SETUP_TOKEN>
```

The access URL is stored in your config file; the token is single-use.

### Plaid

Plaid needs API keys from [dashboard.plaid.com](https://dashboard.plaid.com) (a personal
developer account works).

```bash
moneta setup plaid <CLIENT_ID> <SECRET>            # --env sandbox for testing (default: production)
moneta setup plaid-link                            # prints a URL — finish linking in the browser
```

`plaid-link` links **one institution per run**; re-run it for each bank. Manage linked banks with:

```bash
moneta setup plaid-list                # show linked institutions
moneta setup plaid-unlink <ITEM_ID>    # unlink (stops Plaid billing); synced data stays in the db
```

Notes:
- Plaid replays its full history (up to 730 days) on every sync, so plain `moneta sync`
  suffices after linking — no `--full` needed for Plaid.
- Plaid supplies real account types, which take precedence over moneta's name-based inference.
- If one bank's login expires, that bank is skipped with a warning; the rest of the sync proceeds.
  Re-run `moneta setup plaid-link` for that bank to restore access.

## Syncing

```bash
moneta sync            # incremental: resumes from your newest transaction (with overlap)
moneta sync --full     # re-pull all history — use after linking a new SimpleFIN account
moneta status          # did the last sync work? when, success/failure, what changed
```

A sync runs the full pipeline: ingest → merchant normalization → transfer dedup →
financing-account detection → LLM auto-review → recurring detection → LLM verification →
series events. The summary line tells you what changed (new transactions, transfers linked,
new series, events); if items need your attention (including a financing check) it points
you at `moneta review`.

Every sync is recorded — success or failure — and a failed sync leaves your data untouched.

There is no built-in scheduler. To sync nightly, use cron/launchd, e.g.:

```
0 6 * * * cd ~/code/private/moneta && uv run moneta sync
```

## Reading the numbers

### `moneta power` — spending power

The flagship view for the current month:

- **Income** — detected recurring inflows (paychecks), itemized by source.
- **Fixed costs** — detected recurring outflows (rent, subscriptions, loan payments), itemized.
  Credit-card *payments* are excluded — the purchases behind them are counted instead, so
  nothing is double-counted.
- **Spending power** = income − fixed costs.
- **Spent so far / remaining** — discretionary spend this month (accrual: credit purchases
  count when made, not when the card is paid).

### `moneta networth`

Liquid balances + **vested** holdings − liabilities. Unvested shares are listed separately as
*potential* and never summed in. Accounts in a non-primary currency are excluded and reported.

### `moneta cashflow [--start YYYY-MM-DD] [--end YYYY-MM-DD]`

Accrual spend vs. actual cash out for a range (defaults to this month) — how much you
committed vs. how much left your accounts.

### `moneta recurring`

```bash
moneta recurring                  # detected series: ID, merchant, direction, cadence, amount, next expected
moneta recurring --events         # recent events: missed payments, price increases, new series
moneta recurring --end <ID>       # cancel a series you know is over (IDs are in the table)
moneta recurring --not-a-bill <ID> # override: not recurring — ends the series, suppressed forever
moneta recurring --habit <ID>      # override: discretionary habit, not a bill; reactivates if ended
moneta recurring --re-review <ID>  # reopen the series' bill/habit/not-recurring review question
```

Series end automatically when charges stop, and revive if a genuinely new charge appears at
the old cadence. A price increase is only applied after two agreeing charges (or an LLM/your
confirmation) — a single weird charge never rewrites what a bill is "supposed to" cost.

`--end`, `--not-a-bill`, `--habit`, and `--re-review` are mutually exclusive with each other —
pass exactly one. Each overrule flag writes through the same `recurring_cluster` review-item
ledger a bill/habit/not-recurring answer would (`resolved_by: "manual"`), so a wrong LLM or
detection verdict is always human-correctable: `--not-a-bill` ends the series immediately and
suppresses it from every future sync; `--habit` flips it to discretionary spending and
reactivates it if it had ended; `--re-review` reopens the ledger item so it reappears in
`moneta review` (and a fresh answer can supersede the old one). `--re-review` only reopens
the question — it never changes the series itself, so an ended series stays ended until you
answer `h` (habit, reactivates) or bump it back with the API's status PATCH.

### `moneta obligations`

Loans and 0%-promo financing, fully derived from observed transfers: monthly payment, balance,
estimated months left, and a **deferred interest** warning when the payoff estimate lands after
the promo expiry.

The monthly payment is derived **per loan/financing account** from its transfer-linked
payments, grouped by which account the money lands in — not by the bank's payment
descriptor. This matters because banks often collapse several different cards' payments
into one shared descriptor (e.g. `"Synchrony Bank Payment Web Id"` for three unrelated
store cards); grouping by descriptor would blend or lose individual payments, but grouping
by the linked account keeps each obligation's amount and cadence correct.

### Financing cards

Some store credit cards (e.g. Synchrony-issued cards) are used purely as 0%-promo
financing vehicles rather than for everyday spending. Moneta detects this from behavior,
not the card issuer: a `credit`-typed account whose transaction history is (almost)
nothing but repeated, near-equal payment credits against a positive owed balance — no
real purchase activity since the payments began (an initial financed purchase before the
first payment is fine) — looks like financing in use. The first time this fingerprint
fires for an account, `moneta review` asks a one-time financing-check question (`y`/`n`);
the answer is remembered and never asked again for that account. Answering `y` sets
`financing_mode`, which gives the account loan semantics: its payments count as fixed
costs (instead of its purchases), and it appears in `moneta obligations`.

You can also set or clear this manually, bypassing the detection question:

```bash
moneta accounts --set-financing <ID> true|false
```

After confirming financing this way (review answer or `--set-financing`), any old
credit-payment series over that card is replaced by the derived per-account payment line
on the next `moneta sync` — numbers self-heal automatically, with no double-counting in
the interim.

**Limitation:** a *hybrid* card — one carrying both everyday spending and a promo
financing plan at the same time — can't be split apart from transaction data alone, so
the fingerprint won't fire cleanly and moneta can't separate "this payment is financing"
from "this payment is a normal statement balance." Treat financing-mode as correct only
for cards used exclusively as financing vehicles; a hybrid card should stay a plain
`credit` account (or be handled with `--set-financing false` if it's misdetected).

### `moneta accounts`

```bash
moneta accounts                                    # list accounts with IDs, types, balances
moneta accounts --set-type <ID> <TYPE>             # checking|savings|credit|brokerage|loan|unknown
moneta accounts --set-promo <ID> YYYY-MM-DD        # promo expiry for a financing account
moneta accounts --set-financing <ID> true|false    # mark a credit card as financing (loan semantics)
```

Type overrides survive re-syncs. `--set-promo` and `--set-financing` are manual fields —
promo powers the deferred-interest warning; financing-mode gives a credit account loan
semantics (its payments count as fixed costs instead of its purchases) without waiting on
the `financing_account` review prompt. A financing-mode account shows as `credit (financing)`
in the Type column.

## The review queue

When moneta (and the LLM, if configured) can't classify something confidently, it asks you
instead of guessing:

```bash
moneta review
```

You'll see an upfront summary, then one question at a time:

| Question type | How to answer |
|---|---|
| Transfer match ("which inflow matches…") | Type the number of the right candidate, or Enter to skip |
| Bill, habit, or not recurring ("Is X a recurring bill?") | `b` bill (fixed cost) / `h` habit (discretionary, still tracked) / `n` not recurring |
| Financing check ("X looks like promo financing being paid down…") | `y` treat its payments as fixed costs / `n` leave it as a plain credit card |
| Price change ("Did X change price from $A to $B?") | `y` (applies the new amount) / `n` |
| Merchant name | Type the real merchant name, or Enter to skip |

Skipped items come back next time. Invalid input skips cleanly — nothing is guessed on your
behalf. Answers are recorded with who resolved them (`manual` vs `llm`) so every
classification is auditable.

## LLM assist (optional)

Set `MONETA_LLM_MODEL` to any [LiteLLM model string](https://docs.litellm.ai/docs/providers)
with the provider's API key in your environment:

```bash
export MONETA_LLM_MODEL="anthropic/claude-sonnet-4-5"
export ANTHROPIC_API_KEY=…
```

With an LLM configured, sync auto-resolves review items it's confident about, double-checks
newly detected recurring series, and gates price changes — anything it isn't sure of still
comes to you. **The LLM only ever classifies; it never computes a money value.** Unset the
variable and everything degrades gracefully to deterministic rules + your review queue.

## RSU vesting import

Net worth needs to know which shares are vested. Export a CSV with header
`symbol,vested_quantity,unvested_quantity` (e.g. from Fidelity NetBenefits) and import it:

```bash
moneta import vesting shares.csv
```

Re-import whenever a vest happens.

## Server mode & remote access

By default the CLI needs no server. To run one (e.g. to sync on a home server and query from
a laptop):

```bash
moneta serve                       # binds 127.0.0.1:8300
moneta serve --host 0.0.0.0        # public bind — refused unless MONETA_API_TOKEN is set
```

Every money field in an API response is an integer number of cents, named `*_cents`
(e.g. `"remaining_cents": 355568` is $3,555.68) — clients do their own formatting.

Point any CLI at it:

```bash
export MONETA_API_URL=http://your-server:8300
export MONETA_API_TOKEN=<same token as the server>   # sent as a Bearer header
```

With a token set, every request requires `Authorization: Bearer <token>`, and the interactive
API docs are disabled. This is financial data: prefer localhost or a private network
(Tailscale etc.) even with the token.

## Backup

```bash
moneta backup                 # timestamped snapshot next to the database
moneta backup ~/snap.db       # explicit destination (server-side path in server mode)
```

Snapshots use SQLite `VACUUM INTO` — safe while moneta is running, written owner-only (0600),
and never overwrite an existing file. To restore: stop the server, replace
`~/.config/moneta/moneta.db` with the snapshot, start again (the schema migrator brings an
older snapshot up to date automatically).

## Configuration reference

Config lives in `~/.config/moneta/config.toml`; environment variables (`MONETA_*`) override
file values. The `setup` commands write the file for you.

| Env var / TOML key | Purpose |
|---|---|
| `MONETA_SIMPLEFIN_ACCESS_URL` / `simplefin_access_url` | SimpleFIN access URL (written by `setup simplefin`) |
| `MONETA_PLAID_CLIENT_ID` / `plaid_client_id` | Plaid client id |
| `MONETA_PLAID_SECRET` / `plaid_secret` | Plaid secret |
| `MONETA_PLAID_ENV` / `plaid_env` | `production` (default) or `sandbox` |
| `MONETA_LLM_MODEL` / `llm_model` | LiteLLM model string; unset = no LLM |
| `MONETA_API_URL` / `api_url` | Point the CLI at a remote server |
| `MONETA_API_TOKEN` / `api_token` | Bearer token (server enforcement + client header) |
| `MONETA_DB_PATH` / `db_path` | Database file (default `<config dir>/moneta.db`) |
| `MONETA_CONFIG_DIR` | Config directory (default `~/.config/moneta`) |

Files in the config dir (all owner-only, dir is 0700):

- `config.toml` — settings, including credentials
- `moneta.db` — the database
- `plaid_items.json` — linked Plaid banks (item ids + access tokens)
- `moneta.log` — rotating log file

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Did my sync run last night?" | `moneta status` — shows the last run, success/failure, and the error if it failed. |
| Sync failed | `moneta status` for the error; details in `~/.config/moneta/moneta.log`. Failures never corrupt data — fix the cause and re-sync. |
| A bank's history is missing | `moneta sync --full` re-pulls everything the institution retains (needed after adding a SimpleFIN account, or after an outage longer than the 7-day overlap). |
| Account has the wrong type | `moneta accounts --set-type <ID> <TYPE>` — sticks across syncs. |
| Merchant names look wrong | Answer the merchant questions in `moneta review`; after rule improvements, `moneta renormalize` re-applies naming to all history. |
| A subscription shows as active but is cancelled | `moneta recurring --end <ID>`. |
| A series was wrongly confirmed as a recurring bill | `moneta recurring --not-a-bill <ID>` — ends it and suppresses it from every future sync. |
| A recurring series is really discretionary spending | `moneta recurring --habit <ID>` — reactivates it if ended, tags it discretionary (not a fixed cost). |
| An overrule was a mistake | `moneta recurring --re-review <ID>` reopens the question in `moneta review`. |
| A price change looks wrong | It only applies after two agreeing charges or a confirmation; deny it in `moneta review` and the old expected amount stands. |
| Numbers exclude an account | Foreign-currency accounts are excluded from aggregates by design and reported in `networth`. |
| Remote CLI gets 401 | `MONETA_API_TOKEN` on the client must match the server's. |
| Sync says "setup simplefin or setup plaid" | No source is configured — see [Connecting data sources](#connecting-data-sources). |
