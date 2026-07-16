# Moneta — Product Requirements Document

**Status:** living document — reflects what is shipped on `main` plus the prioritized roadmap
**Owner:** Anirudh Lath
**Last updated:** 2026-07-15
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

A FastAPI server owns **all** logic; the typer CLI is a thin HTTP client that runs the app in-process (ASGI) when no server is configured, so the CLI works with zero setup. Data flows: aggregator adapters (SimpleFIN, Plaid, or both merged) → ingest → merchant normalization → transfer dedup → LLM auto-review → recurring detection → LLM verification → series events, all committing into SQLite (WAL, Alembic-migrated). Views (power, net worth, cashflow, financing) are pure reads. Money is integer cents end-to-end; the LLM only ever classifies — it never produces a money value.

## 6. Shipped features

### 6.1 Data sources & sync

| Feature | Description |
|---|---|
| **SimpleFIN adapter** | v1 source. Claims a setup token once (`moneta setup simplefin`), stores the access URL. Deep history is fetched by walking ≤45-day windows backward past the bridge's 90-day cap. |
| **Plaid adapter** | Full alternative/complement to SimpleFIN: credentials via `moneta setup plaid`, banks linked one-at-a-time through Plaid Hosted Link (`plaid-link` prints a URL, polls until the browser flow finishes), managed with `plaid-list` / `plaid-unlink`. Replays its full `/transactions/sync` history (≤730 days) every run; dedup absorbs the overlap. Plaid's inverted sign conventions are normalized at the adapter boundary. Supplies real account types (`type_hint`) that beat keyword inference. Per-item auth failures degrade gracefully instead of failing the sync. |
| **Merged sources** | With both configured, `MergedAdapter` presents them as one source — a single `moneta sync` pulls everything. |
| **Incremental sync with overlap** | Re-syncs resume from the newest stored transaction minus a 7-day overlap; first sync and `sync --full` pull from the epoch (all history the institution retains). |
| **Upstream correction handling** | A re-synced transaction whose amount/description changed updates in place, clears the stale merchant (normalization re-derives it), and drops any transfer link the correction invalidated — without losing its series membership. |
| **Account type inference & overrides** | SimpleFIN gives no types, so they're inferred from name/org keywords; `moneta accounts --set-type` overrides survive re-sync; Plaid type hints win over inference. `moneta accounts --set-financing ID true\|false` manually flags/clears `financing_mode` (shown as `credit (financing)`), the same flag the `financing_account` review prompt sets — a manual escape hatch pending real behavioral detection (see roadmap). |
| **Sync audit trail** | Every `run_sync` writes a `SyncRun` row (started/finished, success or the error, full report). `moneta status` answers "did last night's sync work?"; failures leave domain tables untouched. |

### 6.2 Pipelines (detection & classification)

| Feature | Description |
|---|---|
| **Merchant normalization** | Deterministic rules first; unrecognized descriptors go to the LLM or the review queue. `moneta renormalize` re-applies improved rules to already-synced data (raw payloads are kept for exactly this). |
| **Transfer dedup** | Candidate pairs scored on amount, date proximity, account direction. High-confidence pairs auto-link; ambiguous ones escalate to LLM/review. The inflow leg is always excluded from analysis; the outflow leg too, unless it pays a loan account (loan payments are fixed costs). |
| **Recurring detection** | Cadence (weekly/biweekly/monthly/annual) + amount-stability analysis over normalized merchants, for outflows (bills) *and* inflows (paychecks). Stats are judged on the newest run of occurrences so deep-history gaps don't poison detection. Only primary-currency transactions are grouped. |
| **Series lifecycle** | Stale series (newest occurrence >3 cadence periods old) auto-end and drop out of events and the power view; a genuinely new charge at cadence revives them; backfilled history never does. Manual end via `moneta recurring --end ID`; manual reactivation bumps the next-expected date forward. `--not-a-bill`/`--habit`/`--re-review ID` let a human overrule or reopen a bill/habit/not-recurring verdict through the same `recurring_cluster` ledger and `apply_resolution` path an LLM or `moneta review` answer takes (`resolved_by: "manual"`); `--not-a-bill` ends the series and suppresses it from every future sync, `--habit` reactivates an ended series as discretionary. |
| **Series events** | `missed` (one per empty grace window, catching up every missed period in one sync) and `price_increase`. A price change needs the two newest occurrences to agree (>5% drift from expected, within 5% of each other) — one outlier never rewrites the expected amount. |
| **LLM auto-review** | Confident LLM answers resolve open review items during sync (before detection, so answers shape the same run). Unconfident/malformed answers fall through to the human queue. |
| **LLM series verification** | Second opinion on deterministic detections: each active series is reviewed once; confident-yes is recorded (and feeds detection's force map), anything else opens a human-only review item. The LLM never suppresses a deterministic detection — it's a ledger, not a veto. |
| **LLM price-change gating** | Drift >5% with an LLM configured: confident-yes applies immediately (even on the first occurrence); anything else opens a `price_change` review item whose "yes" resolution applies the deferred amount. Without an LLM, the deterministic two-occurrence rule stands alone. |

### 6.3 Views (the answers)

| Feature | Description |
|---|---|
| **Spending power** (`moneta power`) | The flagship: detected monthly income (itemized by source) − detected fixed costs (itemized), plus spent-so-far and remaining this month. Credit-card payment series are excluded from fixed costs (their purchases are counted instead — no double count). |
| **Net worth** (`moneta networth`) | Liquid + *vested* holdings − liabilities. Unvested reported separately as potential, never summed in. Foreign-currency accounts are excluded and reported as such. |
| **Cashflow** (`moneta cashflow`) | Accrual spend vs. cash out for a date range (default: this month) — keeps "spent so far" honest when purchases go on credit. |
| **Financing obligations** (`moneta obligations`) | Derived, never stored: any loan account with a detected payment series gets payment, balance, months-left = balance ÷ payment, and a deferred-interest warning when payoff lands after `promo_expires_on` (the one optional manual field in the system). |
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

### 6.7 CLI experience

Rich tables with stable IDs everywhere a follow-up action exists (`recurring --end ID` needs the ID column); events show their series' merchant; income is itemized by source in `power`; markup-hostile merchant names render safely; every error path exits cleanly with a message, never a traceback.

## 7. Feature history

| Date | Release | What shipped |
|---|---|---|
| 2026-07-07 | **v1 core** | Design doc; FastAPI + thin CLI; SimpleFIN; ingest/normalize/transfers/recurring/events pipelines; power, networth, financing views; review queue; LLM auto-review; vesting import. |
| 2026-07-08 | **Full-history sync & stale series** | Epoch-based first sync/`--full`, SimpleFIN window-walking, series auto-end/reactivation rules. |
| 2026-07-15 | **CLI UX quick wins** | Series IDs in tables, event merchants, `moneta cashflow` command, itemized income in `power`. |
| 2026-07-15 | **LLM recurring verification** | `verify_series` second-opinion ledger, price-change LLM gating + review flow, human-only flagged items. |
| 2026-07-15 | **Plaid integration** | Plaid adapter (hosted link, full replay, sign normalization, type hints, per-item degradation), `MergedAdapter`, setup/link/list/unlink CLI. |
| 2026-07-15 | **Review hardening** | Alembic migrations, WAL, bearer-token auth, `moneta status` + sync audit rows, `moneta backup`, rotating logs, local-timezone dates, single-currency views, upstream-correction handling, price-change outlier protection, 0600/0700 file hygiene. |
| 2026-07-16 | **API money convention** | Every response money field is integer cents (`*_cents`); no Decimal-as-string or pre-formatted display strings. The CLI owns all formatting (`fmt_money`, one sign format everywhere); `dollars()` is prose-only (LLM prompts, review-question text). |
| 2026-07-16 | **Recurring overrule CLI** | `moneta recurring --not-a-bill/--habit/--re-review ID` (mutually exclusive with each other and `--end`) — `POST /recurring/{id}/{not-a-bill,habit,re-review}` find-or-create the series' `recurring_cluster` ledger item and apply a manual resolution through the existing `apply_resolution` path, so a wrongly-confirmed bill or habit is always human-correctable. |
| 2026-07-16 | **Manual financing-mode override** | `moneta accounts --set-financing ID true\|false` (`PATCH /accounts` gains `financing_mode`); accounts table shows `credit (financing)`. A manual escape hatch alongside the `financing_account` review prompt — behavioral auto-detection is still open (see roadmap). |

## 8. Roadmap

Sourced from `docs/backlog/` (one file per ticket — see each for context and acceptance criteria).

**High**
- Distinguish fixed obligations from habitual discretionary spending (a weekly restaurant is "recurring" but not a fixed cost — prompts and amount-stability signal must separate them).
- Detect financing-mode credit cards from behavior (no purchases + equal periodic payments + owed balance) and give them loan semantics — real Synchrony store cards type as `credit`, hiding their payments from power/obligations.
- Loan payments must derive from per-account transfer links, not merchant strings (banks collapse multiple cards' payments into one descriptor).
- Brokerage accounts without holdings are invisible to net worth.
- Fidelity NetBenefits CSV mapping (direct export → vesting import).
- Transaction drill-down command (inspect what's behind a number).

**Medium**
- Power rows should show per-cycle and monthly-equivalent amounts explicitly (cadence label next to a monthlyized number is ambiguous).
- Safe-to-spend *today* (prorated within the month).
- Upcoming charges surfaced in `power`.
- Sync progress feedback; sync-staleness warning in `status`; per-item sync warnings surfaced to the user.
- Per-source sync window (Plaid's daily replay currently pins the global window near today).
- Ended-series spend visibility; recurring reactivate via CLI; friendlier remote-CLI connection errors.

**Low**
- JSON output flag (scripting); notifications digest; power history over time; transaction categorization; merchant-normalization improvements; transfer-dedup edge cases; Plaid cursor-based incremental sync; Plaid Link update mode; vesting source adapter seam; SimpleFIN adapter injectable clock; test-coverage gaps.

## 9. Risks & open questions

- **NetBenefits has no official API** — vesting truth currently depends on a manual CSV export (high-priority mapping ticket shortens the path).
- **Aggregator data quality** — upstream corrections are handled, but silent gaps in institution history can only be caught by the user (`sync --full` is the recovery tool; staleness warnings are on the roadmap).
- **LLM cost/quality drift** — mitigated by the classification-only boundary, confidence gating, and full degradation to human review; any LiteLLM-supported model can be swapped in via config.
