# Moneta — Personal Finance Design Doc

**Date:** 2026-07-07
**Status:** Approved pending final sign-off
**Author:** Anirudh Lath (design sessions with Claude)

## 1. Problem

Existing personal-finance apps (Origin, Copilot) fail in four specific ways:

1. **Net worth counts unvested RSUs.** Unvested shares are not money; including them inflates net worth and makes the number useless for decisions.
2. **Subscription / recurring-payment detection is unreliable.** Missed subscriptions, false positives, no notification when a price changes.
3. **Inter-account transfers are not deduplicated.** Moving money between own accounts shows up as spending and income, corrupting every downstream number.
4. **No visibility into actual monthly cash outflow.** A Synchrony 0% financed purchase shows as a lump-sum liability instead of the real monthly liquidity hit, so "how much can I actually spend this month?" is unanswerable.

**The headline goal:** answer *monthly spending power = income − fixed costs*, accurately and automatically, so purchases can be made with full information.

## 2. Goals

- One trustworthy number: monthly spending power, plus "spent so far / remaining" within the month.
- Net worth that counts only vested shares; unvested shown separately as potential.
- Reliable recurring detection with events: missed payment, price increase, new subscription.
- Transfers between own accounts fully excluded from spending/income.
- Financing obligations (0% promos, loans) represented as their **monthly payment**, derived automatically from observed data — no manual plan entry.
- Minimal ongoing manual work: a review queue for ambiguous classifications, not data entry.

## 3. Non-goals (v1)

- Budgeting categories/envelopes, goals, investment analysis or performance tracking.
- Multi-user support. Single user, single machine.
- Web or iOS frontend (the architecture accommodates them later).
- Historical import beyond what the aggregator provides.
- Bill pay or any money movement. Read-only.

## 4. Architecture

**Shape:** FastAPI server owns all logic; CLI is a thin client over the HTTP API.

```
┌─────────────┐     HTTP      ┌──────────────────────────────┐
│ moneta CLI  │ ────────────► │ FastAPI server               │
│ (typer+rich)│               │  ├─ sync jobs (aggregator)   │
└─────────────┘               │  ├─ pipelines (dedup,        │
      later:                  │  │   recurring, financing)   │
┌─────────────┐               │  ├─ LLM assist (LiteLLM)     │
│ web / iOS   │ ────────────► │  └─ SQLite via SQLAlchemy    │
└─────────────┘               └──────────────────────────────┘
                                        │
                              ┌─────────┴──────────┐
                              │ Aggregator adapter │
                              │  SimpleFIN (v1)    │
                              │  Plaid (later)     │
                              └────────────────────┘
```

Decisions and rationale:

- **FastAPI from the start, CLI as thin client** (Approach B). Slightly more upfront work than a local-only CLI, but a future web/iOS frontend reuses the same endpoints with zero logic migration.
- **Aggregator adapter interface.** Domain logic never touches SimpleFIN types. `AggregatorAdapter` exposes `list_accounts()`, `fetch_transactions(account, since)`, `fetch_holdings(account)`. SimpleFIN Bridge is the v1 implementation (cheap, no approval process); Plaid can be added without touching pipelines.
- **SQLite via SQLAlchemy (async).** Single user, one machine. All access through the ORM so a later Postgres swap is a connection-string change plus migration.
- **LiteLLM → cloud LLM** for ambiguous classification only (merchant normalization, transfer-pair disambiguation, irregular recurring clusters). Deterministic heuristics run first; the LLM sees only what they can't resolve. Same pattern as the essay-writer app.
- **Fidelity vesting adapter.** Vested/unvested share truth comes from Fidelity NetBenefits, not a manually maintained grant schedule. NetBenefits has no official API — see Risks (§10); the adapter interface isolates whichever mechanism wins (CSV export import first, automation later).

**Stack (per global conventions):** Python 3.13+, uv, ruff (line 100), mypy --strict, pytest + pytest-asyncio, loguru, Pydantic v2, typer + rich, SQLAlchemy 2 async, FastAPI, LiteLLM.

## 5. Data model

| Entity | Purpose | Key fields |
|---|---|---|
| `Account` | One synced account | type (checking, savings, credit, brokerage, loan/financing), aggregator link, balance, optional `promo_expires_on` |
| `Transaction` | Raw transaction + enrichment | date, amount, raw descriptor, normalized merchant, category, account FK |
| `TransferLink` | A matched inter-account pair | two transaction FKs, match confidence, how matched (rule / LLM / manual) |
| `RecurringSeries` | A detected recurring flow (cost **or** income) | merchant, cadence, expected amount ± tolerance, next expected date, direction, status (active / ended) |
| `SeriesEvent` | Change on a series | type (missed, price_increase, new_series), date, details |
| `FinancingObligation` | **Derived** view of a loan-type account | account FK, observed monthly payment (from its RecurringSeries), remaining balance, est. months left = balance ÷ payment |
| `Holding` | Brokerage position | symbol, quantity, price, `vested_quantity`, `unvested_quantity` |
| `ReviewItem` | Queue of ambiguous classifications | subject (transaction / pair / cluster), question, proposed answer, resolution |

Notes:

- `FinancingObligation` is **computed, not entered**. Any loan-type account with a detected recurring payment gets one automatically. The only optional manual input in the entire system is `promo_expires_on` on an account, which enables the deferred-interest warning; everything else works without it.
- Income is just a `RecurringSeries` with direction `inflow` (paychecks detected like any other recurring flow).
- Raw aggregator payloads are stored alongside parsed rows so pipelines can be re-run when logic improves.

## 6. Pipelines

Sync runs on demand (`moneta sync`) and on a schedule. Each stage is idempotent and re-runnable.

### 6.1 Ingest & normalize

Pull accounts/transactions/holdings through the adapter. Deduplicate on aggregator transaction IDs. Normalize merchants: deterministic rules first (strip store numbers, known descriptor patterns), LLM for unrecognized descriptors, results cached in a merchant-alias table so each descriptor is paid for once.

### 6.2 Transfer dedup

Candidate pairs across accounts scored on three signals:

- **Opposite amounts** — exact match
- **Date proximity** — within ±4 business days (ACH lag)
- **Plausible direction** — checking→credit payment, checking↔savings, checking→loan

High-confidence pairs auto-link. Ambiguous cases (e.g. two same-amount candidates in one window) go to the LLM with both descriptors; still-uncertain ones land in the review queue. Linked pairs are excluded from both spending and income everywhere downstream.

### 6.3 Recurring detection

Group by normalized merchant; test each group for cadence (weekly / monthly ±3 days / annual) and amount stability (tolerance band). Clean matches become a `RecurringSeries`. Ambiguous clusters (variable amounts, renamed merchants, irregular billing) go to the LLM with the group's history. Existing series then generate `SeriesEvent`s: missed payment, price increase, new subscription. Detection runs over both outflows (fixed costs) and inflows (income).

### 6.4 Financing derivation

For each loan-type account: attach its detected recurring payment series, compute `months_left = remaining_balance ÷ monthly_payment`. If `promo_expires_on` is set and the payoff estimate lands after it, raise a deferred-interest warning ahead of the cliff.

### 6.5 Vesting sync

Pull vested/unvested quantities per grant from the Fidelity source (§10) into `Holding`. Net worth = liquid balances + vested holdings − liabilities. Unvested value reported separately, never summed in.

## 7. Views

### 7.1 Spending power (flagship — `moneta power`)

```
Income (detected paychecks)            $X,XXX/mo
Fixed costs                           −$X,XXX/mo
  rent, insurance, utilities (avg)
  subscriptions
  loan & financing payments
─────────────────────────────────────
Spending power                         $X,XXX/mo
Spent so far this month               −$XXX
Remaining                              $XXX
```

Every line auto-detected. "Spent so far" = accrual spending this month minus fixed costs and transfers.

### 7.2 Cash-flow views (under the hood, also queryable)

- **Accrual** ("what did I spend"): all purchases in the period regardless of payment method, transfers excluded. True consumption rate.
- **Cash-out** ("what left liquid accounts"): actual outflows from checking/savings — CC *payments*, loan payments, financing payments. CC purchases don't count here (their payment does); no double counting.

The accrual/cash-out distinction is what keeps "spent so far" honest when purchases go on credit.

### 7.3 Net worth (`moneta networth`)

Liquid + vested holdings − liabilities. Unvested shown as a separate "potential" line.

## 8. CLI surface (v1)

| Command | Does |
|---|---|
| `moneta sync` | Run full pipeline against aggregator |
| `moneta power` | Spending-power view (flagship) |
| `moneta networth` | Net worth with vested/unvested split |
| `moneta recurring` | List series; recent events (price increases, missed) |
| `moneta obligations` | Loans/financing: payment, balance, months left, promo warnings |
| `moneta review` | Work the ambiguity queue (approve/correct classifications) |
| `moneta accounts` | List accounts; set `promo_expires_on` |
| `moneta serve` | Run the FastAPI server |

## 9. LLM usage

- **Gateway:** LiteLLM (provider-agnostic, matches essay-writer pattern).
- **Used for:** merchant normalization (uncached descriptors only), transfer-pair disambiguation, irregular recurring clusters.
- **Never used for:** arithmetic, balances, anything in the money path. LLM output is always a classification that lands in normal columns and is auditable/correctable via `moneta review`.
- All prompts include structured context and demand structured (JSON) output validated by Pydantic.

## 10. Risks & open questions

1. **Fidelity NetBenefits has no official API.** v1 fallback: import the NetBenefits CSV export (`moneta import fidelity <file>`) — manual but infrequent (vesting events are quarterly/annual). Later: evaluate SimpleFIN's Fidelity coverage for holdings and browser automation for the vested/unvested split. The `VestingSource` adapter isolates the choice.
2. **SimpleFIN data quality varies by institution.** Descriptor quality, transaction latency, and balance freshness differ. Mitigation: store raw payloads, keep pipelines re-runnable, adapter interface allows Plaid where SimpleFIN is weak.
3. **Recurring detection cold start.** Needs ~2–3 months of history for cadence confidence; SimpleFIN typically provides 90+ days of history on first sync, which should be sufficient. Confidence shown in output until then.
4. **Variable fixed costs** (utilities) use a trailing average with the tolerance band; classified as fixed but flagged as estimates.

## 11. Later

- Plaid adapter where SimpleFIN coverage is weak.
- Web frontend (same API), then iOS.
- Postgres if the dataset or a multi-device setup demands it.
