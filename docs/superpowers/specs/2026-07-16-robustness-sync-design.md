# Robustness & sync — design

**Date:** 2026-07-16
**Backlog tickets:** `medium/recurring-reactivate-cli.md`,
`medium/remote-cli-connection-errors.md`, `medium/sync-staleness-status-command.md`,
`medium/surface-per-item-sync-warnings.md`, `medium/sync-progress-feedback.md`,
`medium/per-source-sync-window.md`, `low/simplefin-adapter-injectable-clock.md`,
`low/transfer-dedup-edge-cases.md`
**Wave:** 4 of 5. Branch `feature/robustness-sync` off main (post PR #10).
Owner-delegated design decisions recorded inline.

## 1. Per-source sync window (the data-loss fix) — decided: dissolve `MergedAdapter`

`MergedAdapter` is the root cause: it hides which adapter owns which accounts, so
`_sync_since` can only compute one global window. Decision: **run_sync iterates
adapters itself; MergedAdapter is deleted.**

- `AggregatorAdapter` protocol gains `source: str` property (`"simplefin"` /
  `"plaid"`; FakeAdapters in tests: `"fake"` etc.).
- `AccountDTO` gains `source: str`; each adapter stamps its own. New
  `Account.source` column (`String, default ""`) — **migration `0004`**,
  `_HEAD` bump. Ingest writes it (and backfills on re-sync since ingest
  updates existing accounts).
- `run_sync(session, adapters: list[AggregatorAdapter], ...)`: for each
  adapter, `since = await _sync_since(session, full, source=adapter.source)`
  (max `posted_on` of txns joined to accounts with that source; falls back to
  the global max when no accounts carry the source yet — first run after
  upgrade — then epoch as today); fetch each, merge snapshots (accounts/txns/
  holdings concatenated), ingest once. Adapter fetch errors: a failing adapter
  logs + contributes a warning (see §4) but doesn't kill the sync **unless it
  is the only adapter** (preserving today's fail-loudly single-source behavior).
- `api._build_adapter` → `_build_adapters(settings) -> list[...]`; `create_app`
  takes the list; `/sync` 400s when empty. `MergedAdapter` and its tests die.
- Result: a SimpleFIN outage longer than the overlap self-heals on the next
  successful sync; Plaid unaffected (ignores `since`).

## 2. Sync warnings surface — `Snapshot.warnings`

`Snapshot` gains `warnings: list[str] = field(default_factory=list)`. SimpleFIN
appends bridge error strings (`data["errors"]`); Plaid appends its
ITEM_LOGIN_REQUIRED skip message (both currently logger-only; keep the logs).
run_sync concatenates into `SyncReport.warnings: list[str]` (plus §1's
adapter-failure warnings). CLI sync prints each as
`[yellow]⚠ {warning}[/yellow]` after the summary line. No behavior change when
healthy. The real Apple Card "auth required" case becomes visible.

## 3. Staleness + `/status`

- New endpoint `GET /status`: `{last_sync_at: datetime | None, last_sync_ok:
  bool | None, accounts: int, open_reviews: int, aggregators: list[str]
  (configured adapter sources), llm_configured: bool}` — booleans/counters
  only, never secrets. `moneta status` renders it PLUS the existing
  `/sync/last` detail (one command, both facts); fresh-DB → "No sync has run
  yet." unchanged.
- `PowerReport` and `NetWorthReport` gain `data_as_of: datetime | None` (newest
  successful SyncRun.finished_at — a read, allowed in views). CLI power/networth
  print a dim footer when `data_as_of` is older than 24h or None:
  `data as of 2026-07-07 — run moneta sync`.

## 4. Remote CLI connection errors

`cli/client.py::_arequest` wraps the request in
`try/except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)` →
`[red]Error:[/red] could not reach {base_url or 'the aggregator'} ({exc})` +
exit 1. This covers BOTH ticket cases: remote server down (client's own
socket) and the in-process path where the SimpleFIN adapter's httpx call
raises through the ASGI app (same exception types propagate). Test:
`MONETA_API_URL` at a dead port → exit 1, clean message, no traceback.

## 5. Sync progress feedback

Ticket's middle option, implemented via loguru (no protocol churn):

- `SimpleFINAdapter.fetch` logs `logger.info("SimpleFIN: fetching {start} – {end}")`
  per window (Plaid already logs per-item).
- CLI `sync`: wrap the request in `console.status("Syncing…")`; in-process mode
  (no `MONETA_API_URL`) additionally registers a temporary loguru sink
  (INFO, module filter `moneta.aggregator`) that updates the status line with
  the latest window message; sink removed in `finally`. Remote mode: spinner
  only. Pure presentation.

## 6. Reactivate CLI

`moneta recurring --reactivate ID` → existing `PATCH /recurring/{id}`
`{"status": "active"}` (endpoint already reactivates via `reactivate_series`).
Joins the mutually-exclusive flag group (`--end`/`--not-a-bill`/`--habit`/
`--re-review`/`--reactivate`); 404 → clean error; help text updated.

## 7. Transfer-dedup edge cases (decided)

- **Greedy loser** (all candidates consumed by higher-confidence links): opens a
  `transfer_pair` ReviewItem (payload as usual, candidates = the original
  candidate ids) instead of silently vanishing.
- **Originally-ambiguous outflows never late-auto-link** (decided): if an
  outflow started with ≥2 candidates, it opens a ReviewItem even when rivals'
  consumption leaves exactly one — conservative, auditable; the review flow
  auto-resolves via LLM when configured anyway. Documented in the module
  docstring.
- **Resolve-twice 500**: `POST /review/{id}/resolve` for `transfer_pair`
  checks for an existing `TransferLink` on either leg first → clean 409
  `{"detail": "transaction already linked"}`; `IntegrityError` no longer
  reachable there.
- **`--set-type` + `--set-promo` atomicity**: CLI validates the promo date
  BEFORE firing either PATCH (reorder only).

## 8. Injectable clock

`SimpleFINAdapter.__init__` gains `today: Callable[[], date] | None = None`
(None → `datetime.now(UTC).date()`); `fetch` uses it for the window anchor.
Protocol untouched. `tests/test_simplefin.py` passes a fixed callable and
drops `_utc_today()` mirroring.

## Out of scope

- SSE/streaming progress; per-item Plaid repair (wave 5 relink ticket);
  webhooks. `Account.source` backfill beyond natural re-sync adoption.

## Docs & tests

Migration 0004 parity via existing test; every ticket's acceptance criteria
become named tests; user-guide: status section, staleness footer, warnings,
`--reactivate`, progress note; PRD feature table/history + roadmap moves;
delete the eight ticket files. CLAUDE.md: MergedAdapter removal (layout +
sync-window bullets), Snapshot.warnings, Account.source.
