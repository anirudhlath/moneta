# moneta

Personal finance app answering one question: **monthly spending power = income − fixed costs**. FastAPI server owns all logic; the typer CLI is a thin HTTP client (in-process ASGI when no server is running). Design doc: `docs/superpowers/specs/2026-07-07-moneta-design.md`.

## Verification

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
```

All four must pass before every commit. Test output must be pristine — a deprecation warning is a failure.

Run it: `uv run moneta --help` (CLI, no server needed — in-process ASGI) or `uv run moneta serve` (real server; see README for the full quickstart).

## Conventions that aren't obvious from the code

- **Money is integer cents everywhere** (`*_cents: int`); `Decimal` only at boundaries via `to_cents`/`from_cents` (models.py). Never float for money. Share quantities on `Holding` are float — shares aren't money.
- **Sign convention:** negative = outflow, positive = inflow (SimpleFIN's convention, kept end-to-end).
- **Plaid inverts both signs** (aggregator/plaid.py): Plaid amounts are positive when money leaves the account and liability balances are positive-owed; the adapter negates both so stored data follows the SimpleFIN convention. `PlaidAdapter.fetch` ignores `since` — it replays `/transactions/sync` from an empty cursor every run (≤730 days; ingest dedup absorbs the overlap), so `sync --full` is a no-op for Plaid.
- **Enum columns load as plain `str`**, not enum instances (columns are `String`-typed). Compare with `==` (StrEnum equals its value); never `is`, never `.name` on loaded values.
- **Pipelines commit; views don't.** `pipelines/*` own their transaction boundary (`session.commit()` inside); `views/*` are pure reads. `vesting.apply_vesting` commits (it's an import pipeline).
- **Pipeline order is load-bearing:** `run_sync` = ingest → normalize → transfers → auto-review (LLM, when configured) → recurring → verify → events. Auto-review runs before detection so confident LLM answers to open review items shape this run's series/exclusions; recurring detection reads transfer links; `verify_series` second-opinions what detection produced before events fire on it; events read series. `detect_recurring` must never rewind `next_expected_on` (it takes `max`) — rewinding re-fires missed events on every sync.
- **Sync window:** first sync and `sync --full` request from the epoch (1970-01-01) — there is deliberately no window constant; re-syncs resume from the newest stored txn minus overlap (`run.py`). The SimpleFIN adapter satisfies a deep `since` by walking ≤45-day windows backward (the beta bridge hard-caps any request to the trailing 90 days of the range) and stops after ~1 year of empty windows; that's transport policy and lives in the adapter, not the pipelines. The window is global: with Plaid configured its daily replay keeps the window pinned near today, so a SimpleFIN outage longer than the overlap needs `sync --full` to backfill (backlog: per-source-sync-window).
- **Series lifecycle:** `detect_recurring` auto-ends a series whose newest occurrence is >3 cadence periods before `today` (ended series are skipped by events and the power view). An ended series — auto or manual — reactivates only when the group's newest significant txn is untagged (`series_id IS NULL`, i.e. genuinely new since the last run) and fresh; backfilled old txns never reactivate. Cadence and amount stats are judged on the newest run of occurrences, not the whole group, so breaks in deep history don't poison detection.
- **LLM boundary (design §9):** every pipeline takes `llm: Classifier | None`. LLM output is classification only — it gates decisions, never supplies a money value; ambiguity degrades to a `ReviewItem` when no LLM is configured. Keep it that way.
- **LLM verification is a ledger, not a veto:** `verify_series` reviews each active series once — a `recurring_cluster` ReviewItem (open or resolved), keyed by `series_key(merchant, direction)` (models.py), is the per-series record. Confident yes → resolved item (feeds detection's force map); anything else → open item with `payload.llm_flagged: true`. Flagged items are human-only (`autoreview_items` skips them), and the series stays active until a human rules — the LLM never suppresses a deterministic detection. Price drift >5% in events is LLM-gated the same way: not confident-yes → `price_change` item; resolving it true applies the txn's amount (with no LLM configured, both paths keep the old fully-deterministic behavior and open nothing).
- **Transfer-link semantics:** the inflow leg of a link is always excluded from analysis; the outflow leg is excluded unless it pays a loan account (loan payments are fixed costs). Credit-payment series exist but are filtered out of fixed costs in the power view (their purchases are counted instead). Shared link classification lives in `queries.classified_links` — extend it rather than re-joining TransferLink at call sites.

## Layout

- `aggregator/` — adapter protocol + SimpleFIN + Plaid (+ `MergedAdapter` when several are configured); DTOs stop at `pipelines/ingest.py`
- `pipelines/` — ingest, normalize, transfers, review (LLM auto-review + `verify_series`), recurring, events, run (orchestrator)
- `views/` — power (flagship), cashflow, networth, financing (obligations are derived, not stored)
- `api.py` — all endpoints; `cli/` — thin client, zero business logic
- `queries.py` — shared cross-table lookups (`classified_links`, `account_type_map`)

## Gotchas

- `httpx.ASGITransport` never fires FastAPI lifespan — the CLI's in-process path runs `init_db` itself (cli/client.py); a real `moneta serve` gets it from lifespan.
- Endpoints resolve `date.today()` at request time; test data must be date-relative (see tests/test_e2e.py's anchoring comment).
- SimpleFIN gives no account types — they're inferred from name/org keywords (`pipelines/ingest.py`); Plaid supplies real types via `AccountDTO.type_hint`, which beats inference; user overrides via `moneta accounts --set-type` survive re-sync.
- Backlog convention: `docs/backlog/<priority>/<kebab-case>.md`; QA items in `docs/qa-backlog/`.
