# Moneta — Product Requirements Document

**Status:** living document — reflects what is shipped on `main` plus the prioritized roadmap
**Owner:** Anirudh Lath
**Last updated:** 2026-07-16
**Deep-dive specs:** [design doc](superpowers/specs/2026-07-07-moneta-design.md) ·
[LLM recurring verification](superpowers/specs/2026-07-09-llm-recurring-verification-design.md) ·
[Plaid integration](superpowers/specs/2026-07-09-plaid-integration-design.md)

---

## 1. Problem & vision

Existing personal-finance apps (Origin, Copilot) fail in four specific ways:

1. **Net worth counts unvested RSUs.** Unvested shares are not money; including them makes the number useless for decisions.
2. **Recurring/subscription detection is unreliable.** Missed payments and price increases go unnoticed; false positives erode trust.
3. **Inter-account transfers are not deduplicated.** Moving money between your own accounts registers as both spending and income, corrupting every downstream number.
4. **Financing hides the real monthly hit.** A 0%-promo purchase (Synchrony-style deferred-interest financing) shows as a lump-sum liability instead of its actual monthly cash outflow.

**Vision:** one honest number — **monthly spending power = income − fixed costs** — detected automatically from bank data, with no data entry and no category busywork. Every other feature exists to make that number (and net worth) trustworthy.

## 2. Goals

- One trustworthy number: monthly spending power, plus spent-so-far / remaining within the month.
- Net worth that counts only vested holdings; unvested shown separately as *potential*.
- Reliable recurring detection with events: missed payment, price increase, new series.
- Transfers between own accounts fully excluded from spending/income.
- Financing obligations represented as their **monthly payment**, derived from observed transfers — never manually entered.
- Minimal ongoing manual work: a review queue for genuinely ambiguous classifications, nothing else.
- Boring reliability: every sync is audited, the database is migratable and backupable, and the numbers are single-currency and local-calendar-day correct.

## 3. Non-goals

- Budgeting categories/envelopes, goals, investment performance tracking.
- Multi-user support (single user, single machine; server mode exists for the owner's own remote access).
- Bill pay or any money movement — strictly read-only against financial institutions.
- A web or iOS frontend (the API-first architecture accommodates one later with zero logic migration).

## 4. Users

One user: the owner. Technical, runs the CLI locally (or against their own server), links their own bank credentials, and answers review questions occasionally. No onboarding funnel, no permissions model beyond OS file permissions and an optional API bearer token.

## 5. Architecture in one paragraph

A FastAPI server owns **all** logic; the typer CLI is a thin HTTP client that runs the app in-process (ASGI) when no server is configured, so the CLI works with zero setup. Data flows: `run_sync` iterates every configured aggregator adapter (SimpleFIN, Plaid, or both — each fetched on its own per-source incremental window) → ingest → merchant normalization → transfer dedup → LLM auto-review → recurring detection → LLM verification → series events, all committing into SQLite (WAL, Alembic-migrated). Views (power, net worth, cashflow, financing) are pure reads. Money is integer cents end-to-end; the LLM only ever classifies — it never produces a money value.

## 6. Shipped features

### 6.1 Data sources & sync

| Feature | Description |
|---|---|
| **SimpleFIN adapter** | v1 source. Claims a setup token once (`moneta setup simplefin`), stores the access URL. Deep history is fetched by walking ≤45-day windows backward past the bridge's 90-day cap. |
| **Plaid adapter** | Full alternative/complement to SimpleFIN: credentials via `moneta setup plaid`, banks linked one-at-a-time through Plaid Hosted Link (`plaid-link` prints a URL, polls until the browser flow finishes), managed with `plaid-list` / `plaid-unlink`. Replays its full `/transactions/sync` history (≤730 days) every run; dedup absorbs the overlap. Plaid's inverted sign conventions are normalized at the adapter boundary. Supplies real account types (`type_hint`) that beat keyword inference. Per-item auth failures degrade gracefully instead of failing the sync. |
| **Per-source sync windows** | `run_sync` iterates every configured adapter itself (no merging layer): each source's incremental window is computed from *that source's own* newest stored transaction (`Account.source`, stamped by every adapter and migrated in place), not one global window. A stalled or outage-affected source self-heals on its next successful sync instead of being masked by another source's daily full-history replay (e.g. Plaid). A source failing to fetch degrades to a warning and the sync continues on the rest — unless it's the only configured source (or every source fails), in which case the sync fails as before. |
| **Incremental sync with overlap** | Each source resumes from *its own* newest stored transaction minus a 7-day overlap; first sync and `sync --full` pull from the epoch for every source (all history each institution retains). A brand-new source added to an already-populated database inherits the existing global newest date on its first run rather than the epoch — `sync --full` after adding a source ensures full history. |
| **Sync warnings surfaced** | Adapter-level problems that used to be log-only (a SimpleFIN bridge error, a Plaid item needing re-link) are collected on `Snapshot.warnings` → `SyncReport.warnings` and printed by `moneta sync` as `⚠ {warning}` in yellow, right in the sync summary. |
| **Upstream correction handling** | A re-synced transaction whose amount/description changed updates in place, clears the stale merchant (normalization re-derives it), and drops any transfer link the correction invalidated — without losing its series membership. |
| **Account type inference & overrides** | SimpleFIN gives no types, so they're inferred from name/org keywords; `moneta accounts --set-type` overrides survive re-sync; Plaid type hints win over inference. `moneta accounts --set-financing ID true\|false` manually flags/clears `financing_mode` (shown as `credit (financing)`), the same flag the `financing_account` review prompt sets. |
| **Financing-mode detection** | A deterministic fingerprint over `credit`-typed accounts — repeated near-equal payment credits against a positive owed balance, with no real purchase debits since payments began — opens a one-time `financing_account` review question rather than silently reclassifying. A confirmed account gets loan semantics: its payments count as fixed costs and it appears in `obligations`. Daily-use accounts (purchases dominate) never trigger it. Known limitation: a hybrid card carrying both everyday spend and a promo plan can't be split from transaction data alone. |
| **Sync audit trail** | Every `run_sync` writes a `SyncRun` row (started/finished, success or the error, full report). `moneta status` answers "did last night's sync work?"; failures leave domain tables untouched. |
| **`/status` + staleness** | `GET /status` reports `last_sync_at`/`last_sync_ok`, account count, open review-item count, configured `aggregators`, and whether an LLM is configured (booleans/counts only, never secrets). `moneta status` renders it alongside the existing `/sync/last` detail. `power` and `networth` gain `data_as_of` (newest successful sync) and print a dim footer (`data as of {date} — run moneta sync`, or `no successful sync yet`) whenever it's missing or older than 24h — silent otherwise. |
| **Sync progress feedback** | `moneta sync` shows a `Syncing…` spinner; running in-process it live-updates with the current fetch window (e.g. `SimpleFIN: fetching 2026-05-01 – 2026-06-15`) via a temporary loguru sink, removed when the sync finishes. |
| **Clean connection errors** | The CLI catches `ConnectError`/`ConnectTimeout`/`ReadTimeout` around every request and prints `Error: could not reach {target} (...)` plus a clean exit — covers both a down remote server and an unreachable aggregator in in-process mode — instead of a raw traceback. |

### 6.2 Pipelines (detection & classification)

| Feature | Description |
|---|---|
| **Merchant normalization** | Deterministic rules first; unrecognized descriptors go to the LLM or the review queue. `moneta renormalize` re-applies improved rules to already-synced data (raw payloads are kept for exactly this). |
| **Transfer dedup** | Candidate pairs scored on amount, date proximity, account direction. High-confidence pairs auto-link; ambiguous ones escalate to LLM/review. Both legs of every link are unconditionally excluded from recurring-merchant grouping and from spent-so-far — loan-like payments (to a `loan`-type or financing-mode account) are derived per-account from the links instead (see below), not folded into a merchant series. |
| **Transfer dedup edge cases** | Greedy matching, decided deliberately rather than left to fall through: an outflow whose every candidate got consumed by a higher-confidence group ("greedy loser") opens a `transfer_pair` review item instead of silently vanishing; an outflow that started with 2+ candidates never late-auto-links even if rivals consuming their own inflows leave it exactly one — its confidence was computed against the original, more ambiguous set. Resolving an already-linked `transfer_pair` item returns a clean `409 transaction already linked` instead of a server error. |
| **Recurring detection** | Cadence (weekly/biweekly/monthly/annual) + amount-stability analysis over normalized merchants, for outflows (bills) *and* inflows (paychecks). Stats are judged on the newest run of occurrences so deep-history gaps don't poison detection. Only primary-currency transactions are grouped. Amount-unstable groups are classified three ways — **bill** (fixed obligation), **habit** (recurring discretionary spending), or **not recurring** — by an LLM prompt carrying the amount spread, or a human answer in `moneta review`; a habit becomes a `discretionary` series (cadence/price-tracked, never a fixed cost, its transactions count as spend instead). |
| **Per-account loan payment derivation** | For `loan`-type and financing-mode accounts, the monthly payment is derived from transfer-linked outflows **grouped by the inflow (loan) account**, not by merchant descriptor — so a bank collapsing several cards' payments into one shared descriptor (e.g. one issuer's payment string covering three different store cards) still yields one correct payment line per account in `power` fixed costs and `obligations`, instead of a blended-median series or a missing one. |
| **Series lifecycle** | Stale series (newest occurrence >3 cadence periods old) auto-end and drop out of events and the power view; a genuinely new charge at cadence revives them; backfilled history never does. Manual end via `moneta recurring --end ID`; manual reactivation (`moneta recurring --reactivate ID`, or the API's status PATCH) bumps the next-expected date forward to today. `--not-a-bill`/`--habit`/`--re-review ID` let a human overrule or reopen a bill/habit/not-recurring verdict through the same `recurring_cluster` ledger and `apply_resolution` path an LLM or `moneta review` answer takes (`resolved_by: "manual"`); `--not-a-bill` ends the series and suppresses it from every future sync, `--habit` reactivates an ended series as discretionary. Transactions belonging to an ended or discretionary series count toward `spent_so_far` in `power` instead of disappearing from every bucket. |
| **Series events** | `missed` (one per empty grace window, catching up every missed period in one sync) and `price_increase`. A price change needs the two newest occurrences to agree (>5% drift from expected, within 5% of each other) — one outlier never rewrites the expected amount. |
| **LLM auto-review** | Confident LLM answers resolve open review items during sync (before detection, so answers shape the same run). Unconfident/malformed answers fall through to the human queue. |
| **LLM series verification** | Second opinion on deterministic detections: each active series is reviewed once with the same three-way bill/habit/not-recurring classification. Confident "bill" is recorded (and feeds detection's force map); anything else (confident habit/not-recurring, unconfident, or malformed) opens a human-only review item carrying the LLM's leaning for context. The LLM never suppresses a deterministic detection — it's a ledger, not a veto. |
| **LLM price-change gating** | Drift >5% with an LLM configured: confident-yes applies immediately (even on the first occurrence); anything else opens a `price_change` review item whose "yes" resolution applies the deferred amount. Without an LLM, the deterministic two-occurrence rule stands alone. |

### 6.3 Views (the answers)

| Feature | Description |
|---|---|
| **Spending power** (`moneta power`) | The flagship: detected monthly income (itemized by source) − detected fixed costs (itemized), plus spent-so-far and remaining this month. Credit-card payment series are excluded from fixed costs (their purchases are counted instead — no double count). |
| **Net worth** (`moneta networth`) | Liquid + *vested* holdings − liabilities. Unvested reported separately as potential, never summed in. Foreign-currency accounts are excluded and reported as such. |
| **Cashflow** (`moneta cashflow`) | Accrual spend vs. cash out for a date range (default: this month) — keeps "spent so far" honest when purchases go on credit. |
| **Transaction drill-down** (`moneta txns`) | The trust companion to spending power: every transaction in a date range (default: this month; filterable by account/merchant), with `excluded_because` giving the first matching reason a row isn't counted as spend — inflow, transfer, loan payment, credit-card payment, an active non-discretionary fixed-cost series, non-spend account, or foreign currency — mirroring `power`'s spent-so-far rule exactly instead of leaving it a black box. |
| **Financing obligations** (`moneta obligations`) | Derived, never stored: any `loan`-type or financing-mode account with a payment derived from its transfer-linked outflows (per-account, not merchant-string grouped) gets payment, balance, months-left = balance ÷ payment, and a deferred-interest warning when payoff lands after `promo_expires_on` (the one optional manual field in the system). |
| **Single-currency correctness** | A majority-vote primary currency filters all aggregate views and recurring grouping, so mixed-currency accounts can't corrupt the numbers. |
| **Local-calendar-day dates** | Timestamps convert in the process's local timezone — an evening purchase lands on the day the user experienced. |

### 6.4 Human-in-the-loop review

`moneta review` walks the queue interactively with an upfront summary: transfer matches (pick a numbered candidate), recurring-series questions (y/n), price changes (y/n, showing old → new amounts), merchant names (type or skip). Human and LLM resolutions flow through the same auditable path, tagged `resolved_by`. LLM-flagged verification items are human-only — the LLM never answers its own doubts.

### 6.5 The LLM boundary (product principle)

> **Never used for:** arithmetic, balances, anything in the money path. LLM output is always a classification that lands in normal columns and is auditable/correctable via `moneta review`. (Design doc §9)

Provider-agnostic via LiteLLM (any model string), JSON mode, temperature 0, confidence-gated, validated against the real candidate set. Every pipeline takes `Classifier | None`: with no LLM configured — or on any LLM failure — ambiguity degrades to a review item and sync never crashes.

### 6.6 Operations & hardening

| Feature | Description |
|---|---|
| **Server mode** | `moneta serve` (default 127.0.0.1:8300); remote CLI via `MONETA_API_URL`. |
| **Bearer-token auth** | `MONETA_API_TOKEN` enforces `Authorization: Bearer` on every endpoint; `serve` refuses to bind publicly without it; docs/OpenAPI routes are disabled when a token is set. |
| **Backup** | `moneta backup [dest]` — online SQLite snapshot via `VACUUM INTO`, safe while running, written 0600, defaults to a timestamped file next to the DB. Refuses to overwrite. |
| **Migrations** | Alembic owns the schema; `init_db` upgrades to head and adopts pre-Alembic databases by stamping the baseline. A parity test pins migrations against the ORM models. |
| **Durability** | WAL + busy-timeout on file-backed SQLite (server + CLI can write concurrently); config dir 0700 and every file 0600 (credentials, DB, snapshots, logs). |
| **Logging & status** | Rotating log file in the config dir; warnings mirror to stderr; `moneta status` shows the last sync run (including in-flight/incomplete). |
| **Vesting import** | `moneta import vesting file.csv` (`symbol,vested_quantity,unvested_quantity`) — vested truth from a NetBenefits export, not a manually maintained schedule. |
| **Notifications digest** | `moneta digest` (`POST /digest`, migration `0005`'s single-row `digest_state` cursor) pushes new series events and newly-at-risk financing obligations to an [ntfy.sh](https://ntfy.sh) topic (`ntfy_topic` config key). Nothing new → nothing sent (no empty pings), but the event cursor still advances; a delivery failure logs a warning and leaves the cursor untouched so nothing is lost. A cleared deferred-interest risk drops out of the warned-account set so a later re-appearance re-notifies. No `sync --notify` flag — compose it yourself (`moneta sync && moneta digest`, e.g. via cron). Unset `ntfy_topic` is a clean `400` with a setup hint. |

### 6.7 CLI experience

Rich tables with stable IDs everywhere a follow-up action exists (`recurring --end ID` needs the ID column); events show their series' merchant; income is itemized by source in `power`; markup-hostile merchant names render safely; every error path exits cleanly with a message, never a traceback.

## 7. Feature history

| Date | Release | What shipped |
|---|---|---|
| 2026-07-07 | **v1 core** | Design doc; FastAPI + thin CLI; SimpleFIN; ingest/normalize/transfers/recurring/events pipelines; power, networth, financing views; review queue; LLM auto-review; vesting import. |
| 2026-07-08 | **Full-history sync & stale series** | Epoch-based first sync/`--full`, SimpleFIN window-walking, series auto-end/reactivation rules. |
| 2026-07-15 | **CLI UX quick wins** | Series IDs in tables, event merchants, `moneta cashflow` command, itemized income in `power`. |
| 2026-07-15 | **LLM recurring verification** | `verify_series` second-opinion ledger, price-change LLM gating + review flow, human-only flagged items. |
| 2026-07-15 | **Plaid integration** | Plaid adapter (hosted link, full replay, sign normalization, type hints, per-item degradation), `MergedAdapter` (a shim that presented every configured source as one for sync purposes — dissolved 2026-07-16 in favor of per-source sync windows, see below), setup/link/list/unlink CLI. |
| 2026-07-15 | **Review hardening** | Alembic migrations, WAL, bearer-token auth, `moneta status` + sync audit rows, `moneta backup`, rotating logs, local-timezone dates, single-currency views, upstream-correction handling, price-change outlier protection, 0600/0700 file hygiene. |
| 2026-07-16 | **API money convention** | Every response money field is integer cents (`*_cents`); no Decimal-as-string or pre-formatted display strings. The CLI owns all formatting (`fmt_money`, one sign format everywhere); `dollars()` is prose-only (LLM prompts, review-question text). |
| 2026-07-16 | **Three-way recurring classification** | Detection and `verify_series` classify amount-unstable groups as **bill** / **habit** / **not recurring** instead of a plain yes/no — an LLM prompt carrying the amount spread, or a human `b`/`h`/`n` answer in `moneta review`. A `discretionary` flag on `RecurringSeries` marks habits: fully tracked (cadence, price events) but never a fixed cost; their transactions count as spend instead. |
| 2026-07-16 | **Financing-mode fingerprint** | `pipelines/financing.py` deterministically flags `credit`-typed accounts used purely as promo-financing vehicles — repeated near-equal payment credits against a positive owed balance, no real purchase debits since payments began — and opens a one-time `financing_account` review question rather than silently reclassifying; the answer sets `financing_mode` and is never asked again for that account. |
| 2026-07-16 | **Per-account loan payment derivation** | `queries.loan_payment_stats` derives each loan/financing-mode account's monthly payment from its transfer-linked outflows grouped by the **inflow (loan) account**, not by merchant descriptor — fixing the case where a bank collapses several cards' payments into one shared descriptor. `power` shows one fixed-cost line per loan account's payment; `obligations` months-left uses the same per-account figure. |
| 2026-07-16 | **Ended/discretionary spend bucketing** | Transactions tagged to an ended or discretionary series now count toward `spent_so_far` in `power` instead of disappearing from every bucket — a cancelled-but-still-charging subscription, or a habit's spending, was previously invisible money. |
| 2026-07-16 | **Recurring overrule CLI** | `moneta recurring --not-a-bill/--habit/--re-review ID` (mutually exclusive with each other and `--end`) — `POST /recurring/{id}/{not-a-bill,habit,re-review}` find-or-create the series' `recurring_cluster` ledger item and apply a manual resolution through the existing `apply_resolution` path, so a wrongly-confirmed bill or habit is always human-correctable. |
| 2026-07-16 | **Manual financing-mode override** | `moneta accounts --set-financing ID true\|false` (`PATCH /accounts` gains `financing_mode`); accounts table shows `credit (financing)`. A manual escape hatch alongside the `financing_account` review prompt, for correcting or pre-empting the fingerprint detector's verdict. |
| 2026-07-16 | **Per-cycle cadence labels** | `SeriesLine` gains `expected_cents` (per-cycle magnitude); `moneta power` renders non-monthly income/fixed-cost rows as `$265.72 every 2 weeks ≈ $575.73/mo`, monthly rows stay bare, and the merchant cell drops its `(cadence)` suffix entirely — the ambiguity between a per-cycle charge and its monthly-equivalent is resolved in the amount text itself. |
| 2026-07-16 | **Safe-to-spend per day** | `PowerReport` gains `days_left` and signed `per_day_remaining_cents` (`round(remaining / days_left)`, month-end floors `days_left` to 1, no division by zero, negative remaining stays negative — no clamp). `moneta power` renders `Per day (N days left)  $X.YY` right after Remaining. |
| 2026-07-16 | **Upcoming charges** | `PowerReport.upcoming` lists active, non-discretionary, non-cc-payment outflow series with `next_expected_on` in `(today, month_end]`, plus derived loan payments whose projected next date falls in the window — sorted by date. `moneta power` renders a dim `Upcoming this month: X $A.BB (Jul 18) · Y $C.DD (Jul 28)` line under the table; nothing when empty. |
| 2026-07-16 | **Transaction drill-down** | New `views/transactions.py` + `GET /transactions` + `moneta txns [--month M \| --start D --end D] [--account ID] [--merchant NAME]`: every transaction in range, joined to its account/series/transfer-link classification, with `counted_in_spend`/`excluded_because` replicating `power`'s spent-so-far rule row-by-row. CLI dims excluded rows (never hides them) and prints a two-line footer — `Counted as spend` (all counted rows, always) and a dim `Through today (power's spent-so-far)` (only when the range covers today, since that's the only case the two numbers actually agree). |
| 2026-07-16 | **`--json` on every read command** | `power`, `networth`, `cashflow`, `recurring`, `obligations`, `accounts`, `txns`, and `status` accept `--json`, printing the raw API response to stdout with no rich markup — scriptable, pipeable to `jq`. Combining `--json` with a write flag (`recurring --end/--not-a-bill/--habit/--re-review`, `accounts --set-type/--set-promo/--set-financing`) is a clean, request-free error. |
| 2026-07-16 | **Power history** | `GET /power/history?months=N` (1-60, default 6) returns newest-first per-month rows (`income_cents`/`spend_cents` magnitudes, signed `net_cents`); past months report *actual observed* accrual income/spend for that calendar month, not today's recurring-series state, via a new `accrual_income` beside `accrual_spend`. `moneta power --history N` renders a Month/Income/Spend/Net table instead of the current-month view. |
| 2026-07-16 | **Account source attribution** | `Account.source` (migration `0004`) records which adapter (`"simplefin"`/`"plaid"`) owns each account; the `AggregatorAdapter` protocol gains a `source` property and every adapter (and test fake) stamps `AccountDTO.source`; ingest writes it on create and backfills it on update. The prerequisite for per-source sync windows below. |
| 2026-07-16 | **Per-source sync windows; `MergedAdapter` dissolved; warnings surfaced** | `MergedAdapter` deleted: `run_sync(session, adapters: list[...], ...)` iterates every configured adapter itself, computing each one's incremental `since` from *its own* accounts' newest transaction (`_sync_since(..., source=...)`) instead of one global window — fixes the bug where Plaid's daily full-history replay masked a stalled/failing SimpleFIN source from ever showing a stale window. A failing adapter degrades to a warning and the sync continues on the rest, unless it's the sole adapter or every adapter fails (both re-raise, matching the prior fail-loud behavior). `Snapshot.warnings`/`SyncReport.warnings` carry adapter-level problems (SimpleFIN bridge errors, Plaid `ITEM_LOGIN_REQUIRED` skips) that were previously log-only; `moneta sync` prints each as a yellow `⚠` line in the summary. `api._build_adapter` → `_build_adapters(settings) -> list[...]`; `POST /sync` 400s when the list is empty. |
| 2026-07-16 | **Richer `/status`; staleness footers** | New `GET /status`: `last_sync_at`, `last_sync_ok`, `accounts`, `open_reviews`, `aggregators` (configured adapter sources), `llm_configured` — booleans/counts only, never secrets. `moneta status` renders it alongside the existing `/sync/last` run detail; `--json` merges both into one object (`{**status, "last_sync": ...}`). `PowerReport`/`NetWorthReport` gain `data_as_of` (newest successful `SyncRun.finished_at`); `moneta power`/`moneta networth` print a dim footer when it's missing or more than 24h old, silent otherwise. |
| 2026-07-16 | **Clean connection errors; sync progress** | `cli/client.py`'s `_arequest` catches `httpx.ConnectError`/`ConnectTimeout`/`ReadTimeout` around every request and prints `Error: could not reach {target} (...)` with a clean exit 1 — covers a down remote server and an unreachable in-process aggregator alike, replacing a raw traceback. `moneta sync` wraps the request in a `Syncing…` spinner; in-process mode additionally registers a temporary loguru sink (INFO, `moneta.aggregator`) that live-updates the spinner with the current fetch window, removed when the sync finishes. `SimpleFINAdapter.fetch` now logs each window fetch at INFO for this to render. |
| 2026-07-16 | **`--reactivate`; transfer-dedup edge cases; atomic account flags** | `moneta recurring --reactivate ID` (mutually exclusive with `--end`/`--not-a-bill`/`--habit`/`--re-review`) PATCHes a series straight back to active. Transfer-dedup decided two edge cases explicitly: a "greedy loser" (every candidate consumed by a higher-confidence group) opens a `transfer_pair` review item instead of vanishing; an originally-ambiguous outflow (2+ candidates) never late-auto-links even if rivals' consumption leaves exactly one candidate standing. `POST /review/{id}/resolve` on an already-linked `transfer_pair` returns a clean `409` instead of a server error. `moneta accounts --set-type`/`--set-promo` now validates the promo date before firing either PATCH, so a bad date can't partially apply. |
| 2026-07-16 | **Injectable SimpleFIN clock** | `SimpleFINAdapter.__init__` takes an optional `today: Callable[[], date]` (defaults to `datetime.now(UTC).date()`); `fetch`'s window-walk anchors on it. Pure test-seam refactor — the `AggregatorAdapter` protocol is unchanged. |
| 2026-07-16 | **Notifications digest** | `moneta digest` / `POST /digest` (migration `0005`, `digest_state` single-row cursor): pushes new series events and newly-at-risk financing obligations to an ntfy.sh topic (`ntfy_topic` config key, `MONETA_NTFY_TOPIC`). Nothing new advances the cursor without sending; a delivery failure logs a warning and leaves the cursor untouched; a cleared risk drops out of the warned set so it can re-notify later. `--json` is deliberately exempt from `reject_json_with_writes` since printing the POST result is the command's whole point. Cron recipe: `moneta sync && moneta digest` — no `sync --notify` flag. |

## 8. Roadmap

Sourced from `docs/backlog/` (one file per ticket — see each for context and acceptance criteria).

**High**
- Brokerage accounts without holdings are invisible to net worth.
- Fidelity NetBenefits CSV mapping (direct export → vesting import).

**Low**
- Transaction categorization; Plaid cursor-based incremental sync; Plaid Link update mode; vesting source adapter seam; test-coverage gaps.

## 9. Risks & open questions

- **NetBenefits has no official API** — vesting truth currently depends on a manual CSV export (high-priority mapping ticket shortens the path).
- **Aggregator data quality** — upstream corrections are handled, and per-source sync windows plus `moneta status`/staleness footers now surface a stalled or failing source instead of masking it; `sync --full` remains the recovery tool for backfilling once a source is fixed.
- **LLM cost/quality drift** — mitigated by the classification-only boundary, confidence gating, and full degradation to human review; any LiteLLM-supported model can be swapped in via config.
