# Robustness & Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-source sync windows (dissolving MergedAdapter), surfaced sync warnings, staleness/status, clean connection errors, sync progress, `--reactivate`, transfer-dedup edge cases, injectable adapter clock.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-16-robustness-sync-design.md` — READ IT FIRST; all decisions live there. One migration (0004: `Account.source`). Suite green after every task.

## Global Constraints

- Gate before every commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — pristine.
- Schema change = migration + `_HEAD` bump (parity test pins). Pipelines commit; views pure reads; cli/ zero logic. Branch `feature/robustness-sync` off main.
- Each task deletes its shipped ticket(s) + scrubs living-doc mentions in its own commit.

### Task 0: Branch
- [ ] `git checkout -b feature/robustness-sync main`

### Task 1: `Account.source` + adapter `source` + migration 0004 (spec §1 first half)
Files: models.py, migrations/versions/0004_account_source.py, db.py, aggregator/base.py (+DTO), aggregator/simplefin.py, aggregator/plaid.py, pipelines/ingest.py; tests.
- [ ] `AccountDTO.source: str = ""`; SimpleFIN stamps `"simplefin"`, Plaid `"plaid"`; `Account.source: Mapped[str] = mapped_column(default="")`; migration 0004 (`server_default=""`), `_HEAD="0004"`; ingest writes source on create AND update (natural backfill). Protocol gains `source: str` property; concrete adapters + every test fake implement it.
- [ ] Tests: ingest stamps/backfills source; migration parity green. Gate; commit `feat(schema): account source attribution (migration 0004)`.

### Task 2: Dissolve MergedAdapter — per-source windows (spec §1 second half + §2 warnings plumbing)
Files: pipelines/run.py, api.py, aggregator/base.py (delete MergedAdapter, add `Snapshot.warnings`), aggregator/simplefin.py (append bridge errors), aggregator/plaid.py (append skip messages); tests (test_run.py, test_api.py, whatever pins MergedAdapter — rewrite to the list world).
- [ ] `Snapshot.warnings: list[str]` (default empty). `run_sync(session, adapters: list[AggregatorAdapter], llm, today, full)`: per-adapter `_sync_since(session, full, source)` (max posted_on joined via Account.source == source; fallback global max when the source has no accounts; epoch base cases unchanged); fetch each inside try/except — a failing adapter appends `f"{source}: {exc}"` to warnings and continues UNLESS it's the only adapter (re-raise, preserving single-source fail-loud); merge snapshots; `SyncReport.warnings: list[str]` = snapshot warnings + failure warnings. `api._build_adapters -> list`; `create_app(adapters=...)`; `/sync` 400 when empty. Delete MergedAdapter + its tests. CLI sync prints `[yellow]⚠ {w}[/yellow]` per warning.
- [ ] Tests: two fake adapters with distinct sources get distinct `since` values (RecordingAdapter pattern); simplefin-outage self-heal scenario (old simplefin txns + fresh plaid txns → simplefin since derives from ITS newest, not plaid's); one-of-two failure → warning + other source ingested; only-adapter failure → raises + SyncRun failed; warning printed by CLI. Gate; commit `feat(sync): per-source windows; MergedAdapter dissolved; warnings surfaced`. Tickets: per-source-sync-window, surface-per-item-sync-warnings.

### Task 3: `/status` + staleness footers (spec §3)
Files: api.py, views/power.py, views/networth.py, cli/main.py; tests.
- [ ] `GET /status` per spec shape (aggregators = configured sources list; llm_configured = classifier is not None; counts via two selects; last sync from newest successful SyncRun). `data_as_of` on PowerReport+NetWorthReport (newest successful SyncRun.finished_at). CLI `status` renders both /status and /sync/last; power/networth dim footer when stale (>24h or None): `data as of {date} — run moneta sync` / `no successful sync yet — run moneta sync`.
- [ ] Tests: status shape/booleans (no secrets); fresh-DB; footer present when stale, absent when fresh (seed a SyncRun finished now); --json includes data_as_of. Gate; commit `feat(status): richer status endpoint; staleness footers`. Ticket: sync-staleness-status-command.

### Task 4: Connection errors + sync progress (spec §§4-5)
Files: cli/client.py, cli/main.py::sync, aggregator/simplefin.py (window INFO log); tests.
- [ ] `_arequest` catches `(httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)` → red `could not reach {target}` + Exit 1. SimpleFIN logs each window fetch at INFO. CLI sync wraps in `console.status("Syncing…")`; in-process adds a temporary loguru sink (INFO, `moneta.aggregator` filter) updating the status text; removed in finally.
- [ ] Tests: dead-port remote → exit 1 clean, no Traceback (ticket's acceptance); sink registered/removed (in-process sync test still pristine); window log emitted (caplog-style via loguru test handler in test_simplefin). Gate; commit `feat(cli): clean connection errors; sync progress`. Tickets: remote-cli-connection-errors, sync-progress-feedback.

### Task 5: `--reactivate` + transfer-dedup edges (spec §§6-7)
Files: cli/main.py, pipelines/transfers.py, api.py resolve endpoint; tests.
- [ ] `--reactivate ID` joins the exclusive flag group → PATCH active; help text. Transfers: greedy loser opens ReviewItem; originally-ambiguous (≥2 candidates at scoring time) never late-auto-links — opens ReviewItem (module docstring documents the decision); resolve endpoint checks existing link on either leg → 409. CLI accounts validates `--set-promo` date before ANY PATCH fires.
- [ ] Tests: each ticket bullet (loser → item; ambiguous-then-one → item not link; double-resolve → 409 not 500; bad promo + set-type → neither applied, exit 1); reactivate flag + 404 + mutual exclusion. Gate; commit `feat(review,transfers): reactivate flag; dedup edge cases; atomic account flags`. Tickets: recurring-reactivate-cli, transfer-dedup-edge-cases.

### Task 6: Injectable clock (spec §8)
Files: aggregator/simplefin.py, tests/test_simplefin.py.
- [ ] `__init__(..., today: Callable[[], date] | None = None)`; fetch anchors on it; tests pass fixed callable, `_utc_today()` mirroring deleted. Gate; commit `refactor(simplefin): injectable window anchor`. Ticket: simplefin-adapter-injectable-clock.

### Task 7: Docs
- [ ] user-guide (status, staleness, warnings, --reactivate, progress, connection errors troubleshooting row); PRD (feature table/history, roadmap moves); README if command table moves; CLAUDE.md (MergedAdapter gone — run_sync iterates adapters; Account.source; Snapshot.warnings; sync-window bullet rewrite). Verify against code. Gate; commit `docs: wave-4 robustness features`.

### Task 8: Gates + smoke + PR
- [ ] Simplify pass (2 dual-lens agents); final whole-branch review (opus); QA agent; fix loops as needed.
- [ ] REAL-DATA smoke: backup DB (`.bak-pre-wave4`); `moneta status` (new shape), `moneta sync` (progress line, per-source windows, Apple Card warning now VISIBLE in yellow), power footer freshness, `--reactivate` on a test... skip destructive review answers; `MONETA_API_URL=http://127.0.0.1:1 moneta power` → clean error.
- [ ] Push, PR, merge (standing authorization), main updated, ledger closed.
