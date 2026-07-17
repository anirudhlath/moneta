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
  - [`moneta status`](#moneta-status)
  - [Staleness footers](#staleness-footers)
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
moneta sync --full     # re-pull all history — use after linking a new source
moneta status          # is data fresh? did the last sync work? what changed
```

A sync runs the full pipeline: ingest → merchant normalization → transfer dedup →
financing-account detection → LLM auto-review → recurring detection → LLM verification →
series events. The summary line tells you what changed (new transactions, transfers linked,
new series, events); if items need your attention (including a financing check) it points
you at `moneta review`. While a sync is running, the terminal shows a `Syncing…` spinner;
running in-process (the default, no `MONETA_API_URL`) the spinner also live-updates with the
current fetch window (e.g. `Syncing… SimpleFIN: fetching 2026-05-01 – 2026-06-15`) so a long
first sync isn't a silent wait. Pointed at a remote server (`MONETA_API_URL` set) you only see
the spinner — the fetch itself is happening on the server, out of the client's view.

Each configured source (SimpleFIN, Plaid, ...) syncs on its **own** incremental window,
computed from that source's own newest stored transaction — not a single global window. This
means a SimpleFIN outage, or a bank that's been silently failing for weeks, self-heals on the
next successful sync instead of staying masked by Plaid's daily full-history replay. The
tradeoff: a brand-new source's first-ever sync on an already-populated database inherits the
existing global newest date as its starting point rather than pulling full history — run
`moneta sync --full` once after adding a new source to be sure you have everything.

If a source fails to fetch (bad credentials, bridge down, network error) but at least one
other configured source succeeds, the sync still completes — the failure is reported as a
yellow warning, not a hard failure:

```
Synced: 12 new transactions, 1 transfers linked, 0 new series, 0 events.
⚠ simplefin: ConnectError: [Errno 61] Connection refused
```

(A real example: a Plaid item losing its login shows `⚠ Plaid item Apple Card skipped
(ITEM_LOGIN_REQUIRED) — re-link with: moneta setup plaid-link` — previously this only reached
the log file; now it's visible in the sync summary the moment it happens.) Only when **every**
configured source fails does the sync itself fail (nothing gets ingested, so `moneta status`
correctly reports the run as failed rather than a quiet success over an empty pull).

Every sync is recorded — success or failure — and a failed sync leaves your data untouched.

If the CLI can't reach the server (remote mode) or the aggregator itself can't be reached
(in-process mode, e.g. SimpleFIN unreachable), you get a clean one-line error instead of a
traceback: `Error: could not reach http://your-server:8300 (...)` or
`Error: could not reach a configured aggregator (...)`, followed by a non-zero exit.

There is no built-in scheduler. To sync nightly, use cron/launchd, e.g.:

```
0 6 * * * cd ~/code/private/moneta && uv run moneta sync
```

### `moneta status`

One command answers both "is moneta healthy?" and "did last night's sync work?":

```bash
moneta status
```

```
Last sync: 2026-07-16T06:00:03 → ok
  12 new txns, 1 new series, 0 events
Accounts: 8  ·  Open review items: 2
Aggregators: simplefin, plaid  ·  LLM: configured
```

The first two lines come from the most recent sync run (as before): timestamp, outcome
(`ok` / `failed` — with the error / `incomplete` — still running or the process died
mid-sync), and a one-line summary of what changed. The last two lines are new: total
accounts, open review-queue items, which sources are configured (`aggregators`, from
`AggregatorAdapter.source` — `simplefin` and/or `plaid`), and whether an LLM is configured.
These are booleans/counts only — never credentials. On a fresh database with no sync yet,
you still see the accounts/reviews/aggregators/LLM summary; only the "Last sync" line is
replaced with "No sync has run yet."

`moneta status --json` merges both API calls into one object: the `/status` fields
(`last_sync_at`, `last_sync_ok`, `accounts`, `open_reviews`, `aggregators`, `llm_configured`)
plus a `last_sync` key holding the full `/sync/last` detail (`null` if no sync has ever run):

```json
{"last_sync_at": "2026-07-16T06:00:03", "last_sync_ok": true, "accounts": 8,
 "open_reviews": 2, "aggregators": ["simplefin", "plaid"], "llm_configured": true,
 "last_sync": {"status": "ok", "started_at": "...", "finished_at": "...",
               "success": true, "error": null, "report": {...}}}
```

### Staleness footers

`moneta power` and `moneta networth` print a dim footer whenever the data behind them isn't
fresh — either no sync has ever succeeded, or the newest successful sync is more than 24 hours
old:

```
data as of 2026-07-14 — run moneta sync
```

or, on a database that's never synced:

```
no successful sync yet — run moneta sync
```

The footer is silent (nothing printed) once you've synced within the last 24 hours — it only
speaks up when the numbers you're looking at might be stale.

## Reading the numbers

### `moneta power` — spending power

The flagship view for the current month:

- **Income** — detected recurring inflows (paychecks), itemized by source.
- **Fixed costs** — detected recurring outflows (rent, subscriptions, loan payments), itemized.
  Credit-card *payments* are excluded — the purchases behind them are counted instead, so
  nothing is double-counted.
- Non-monthly rows (income or fixed costs) show both numbers explicitly, e.g.
  `$2500.00 every 2 weeks ≈ $5416.67/mo` — monthly rows stay a bare amount.
- **Spending power** = income − fixed costs.
- **Spent so far / remaining** — discretionary spend this month (accrual: credit purchases
  count when made, not when the card is paid).
- **Per day (N days left)** — `remaining ÷ days left in month` (today through month-end,
  inclusive; the last day of the month shows 1 day left). Negative remaining shows a negative
  per-day figure — no clamping to zero.
- **Upcoming this month** — a dim line under the table listing active, non-discretionary,
  non-credit-card-payment fixed costs (plus derived loan payments) due later this month, e.g.
  `Upcoming this month: Rent $1400.00 (Jul 15) · Netflix $15.99 (Jul 28)`. Omitted when nothing
  is due before month-end.

`moneta power --history N` (e.g. `--history 6`, 1-60 months) replaces the table above with a
compact Month/Income/Spend/Net history instead. The newest row is the current, still-in-progress
month; every row (including that one) reports *actual observed* income and spend for that
calendar month — not today's recurring-series state — so a since-cancelled bill or an old raise
still shows correctly even though no series reflects it anymore.

### `moneta txns` — auditing the numbers

`moneta power`'s "Spent so far" is a single number; `moneta txns` is how you check it —
every transaction in a date range, with the exact reason it is or isn't counted as spend:

```bash
moneta txns                          # this month, every transaction
moneta txns --month 2026-06          # a specific month
moneta txns --start 2026-06-01 --end 2026-06-15 --account 3
moneta txns --merchant netflix       # case-insensitive substring match
```

`--month` and `--start`/`--end` are mutually exclusive. Counted rows show a plain `✓`;
excluded rows render dim with the reason in the same column — one of:

| Reason | Meaning |
|---|---|
| `inflow` | Money coming in, not spend |
| `transfer` | The inflow leg of an internal transfer (always excluded) |
| `loan payment` | Outflow leg of a transfer into a loan/financing account (counted as a fixed cost instead, in `power`) |
| `credit-card payment` | Outflow leg of a transfer paying a credit card (the underlying purchases are counted instead) |
| `fixed cost (series X)` | Tagged to an active, non-discretionary recurring series (counted in `power`'s fixed costs instead) |
| `non-spend account` | Posted to an account type that isn't spend-eligible (e.g. a loan or brokerage account) |
| `foreign currency` | Posted to a non-primary-currency account (excluded from all aggregates) |

`excluded_because` is the *first* matching reason in that order — a row only ever gets one.

The footer has two lines:

- `Counted as spend: -$X.YY` — always printed; the sum of every counted row in the
  requested range, regardless of date.
- `Through today: -$X.YY` (dim) — printed only when the requested range includes today;
  sums counted rows dated on or before today. On the unfiltered current-month view (no
  `--account`/`--merchant`) this line reads `Through today (power's spent-so-far): -$X.YY`
  and is the number that matches `moneta power`'s "Spent so far" exactly (both clamp to
  today, over every account). Add `--account` or `--merchant` and the parenthetical drops —
  the total is now scoped to the filter, so it no longer equals `power`'s whole-portfolio
  number even though it's still "through today" for that filter. The first line does *not*
  claim any parity — a full-month view includes future-dated rows that `power` hasn't
  counted yet, and a past month has no "through today" concept at all.

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
moneta recurring --reactivate <ID> # bring an auto- or manually-ended series back to active
```

Series end automatically when charges stop, and revive if a genuinely new charge appears at
the old cadence. A price increase is only applied after two agreeing charges (or an LLM/your
confirmation) — a single weird charge never rewrites what a bill is "supposed to" cost.

`--end`, `--not-a-bill`, `--habit`, `--re-review`, and `--reactivate` are mutually exclusive
with each other — pass exactly one. `--end`/`--not-a-bill`/`--habit`/`--re-review` each write
through the same `recurring_cluster` review-item ledger a bill/habit/not-recurring answer
would (`resolved_by: "manual"`), so a wrong LLM or detection verdict is always
human-correctable: `--not-a-bill` ends the series immediately and suppresses it from every
future sync; `--habit` flips it to discretionary spending and reactivates it if it had ended;
`--re-review` reopens the ledger item so it reappears in `moneta review` (and a fresh answer
can supersede the old one). `--re-review` only reopens the question — it never changes the
series itself, so an ended series stays ended until you answer `h` (habit, reactivates) or
bring it back directly.

`--reactivate ID` is that direct path: it flips an ended series (auto-ended from inactivity,
or ended via `--end`) straight back to active and bumps `next_expected_on` forward to today,
with no review-queue detour — use it when you already know a bill you previously ended (or
that auto-ended after a gap) has resumed. A 404 for an unknown ID prints a clean error.

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

### Scripting: `--json`

Every read command above (`power`, `networth`, `cashflow`, `recurring`,
`obligations`, `accounts`, `txns`, `status`) accepts `--json`: it prints the
raw API response to stdout with no rich markup, ready to pipe into `jq` or a
script, and returns before any table is built. `moneta status --json` merges
`GET /status` and `GET /sync/last` into one object — the `/status` fields
(`last_sync_at`, `last_sync_ok`, `accounts`, `open_reviews`, `aggregators`,
`llm_configured`) plus a `last_sync` key holding the full `/sync/last` payload,
which is `null` when no sync has ever run (the rest of the object is still
populated). Combining `--json` with a write flag
(`recurring --end/--not-a-bill/--habit/--re-review/--reactivate`,
`accounts --set-type/--set-promo/--set-financing`) is a clean error — no
request fires.

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

Fields split into three sign conventions, decided per field rather than globally:

- **Signed** (storage convention — negative = outflow, positive = inflow): `balance_cents`
  (accounts), `expected_cents` (recurring series), `TxnRow.amount_cents`, `net_worth_cents`,
  `spending_power_cents`, `remaining_cents`, `per_day_remaining_cents`, `PowerMonth.net_cents`,
  and review-context amounts (`amount_cents` on samples/candidates,
  `old_amount_cents`/`new_amount_cents`).
- **Signed sums** (a plain sum of underlying signed values, not forced positive — can go
  negative, e.g. an overdrawn account makes `liquid_cents` negative): `liquid_cents`,
  `vested_holdings_cents`.
- **Unsigned magnitudes** (a labeled quantity, e.g. "Fixed costs" or "Liabilities" — the label
  carries the direction, not the sign): `monthly_income_cents`, `total_fixed_cents`,
  `spent_so_far_cents`, `SeriesLine.monthly_cents`, `SeriesLine.expected_cents`,
  `UpcomingCharge.expected_cents`, `liabilities_cents`, `unvested_potential_cents`,
  `accrual_cents`, `cash_out_cents`, `balance_owed_cents`, `monthly_payment_cents`,
  `PowerMonth.income_cents`, `PowerMonth.spend_cents`.

New money fields document their sign here as they ship. The CLI never hand-negates a
magnitude field — it calls `fmt_outflow(magnitude_cents)` (renders with the display minus)
instead of `fmt_money(-magnitude_cents)`.

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
| One source failed but the sync "succeeded" | That's by design when at least one other source is configured and healthy — check the `⚠` warning line the sync prints (or `moneta status`/the log) for which source and why; a source only fails the whole sync when it's the only one configured, or every configured source fails together. |
| A bank's history is missing | `moneta sync --full` re-pulls everything the institution retains for every configured source (needed after adding a new source, or after an outage longer than the 7-day overlap). |
| Account has the wrong type | `moneta accounts --set-type <ID> <TYPE>` — sticks across syncs. |
| Merchant names look wrong | Answer the merchant questions in `moneta review`; after rule improvements, `moneta renormalize` re-applies naming to all history. |
| A subscription shows as active but is cancelled | `moneta recurring --end <ID>`. |
| A series was wrongly confirmed as a recurring bill | `moneta recurring --not-a-bill <ID>` — ends it and suppresses it from every future sync. |
| A recurring series is really discretionary spending | `moneta recurring --habit <ID>` — reactivates it if ended, tags it discretionary (not a fixed cost). |
| A series ended but the bill actually resumed | `moneta recurring --reactivate <ID>` — flips it straight back to active, no review-queue detour. |
| An overrule was a mistake | `moneta recurring --re-review <ID>` reopens the question in `moneta review`. |
| A price change looks wrong | It only applies after two agreeing charges or a confirmation; deny it in `moneta review` and the old expected amount stands. |
| Numbers exclude an account | Foreign-currency accounts are excluded from aggregates by design and reported in `networth`. |
| `power`/`networth` show a dim "data as of ..." line | The newest successful sync is more than 24h old (or there's never been one) — run `moneta sync`. See [Staleness footers](#staleness-footers). |
| `Error: could not reach ...` on any command | Remote mode: the server at `MONETA_API_URL` is down or unreachable — check it's running and the URL/port are right. In-process mode: the aggregator itself (e.g. SimpleFIN) couldn't be reached — check your network. Either way this is a clean exit, not a traceback. |
| Remote CLI gets 401 | `MONETA_API_TOKEN` on the client must match the server's. |
| Sync says "setup simplefin or setup plaid" | No source is configured — see [Connecting data sources](#connecting-data-sources). |
