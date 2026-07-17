# Sync progress spinner renders live during a real slow --full walk

**Feature:** Sync progress feedback (`moneta sync`'s `console.status` spinner + temporary loguru sink, `src/moneta/cli/main.py`)
**Priority:** medium
**Type:** functional

## Prerequisites
- A real SimpleFIN Bridge connection with enough transaction history that `--full`'s
  ≤45-day window walk takes visibly long (many windows, not one or two)
- A real interactive terminal (TTY) — this feature is specifically about `rich`'s live
  spinner region, which `typer`'s `CliRunner` (used by all automated tests) captures
  non-interactively and cannot exercise
- Optional: Plaid also configured, to observe the handoff between sources

## Test Steps
1. Run `uv run moneta sync --full` in a real terminal, in-process mode (no `MONETA_API_URL`
   set — that's the path that registers the progress sink).
2. Watch the spinner: it should start as `Syncing…` and update live to
   `Syncing… SimpleFIN: fetching {start} – {end}` as each window is fetched, with the
   date range visibly moving backward over the course of the sync.
3. Confirm no flicker, garbled text, or stale window text left behind between updates.
4. If Plaid is also configured, watch what happens once SimpleFIN's fetch finishes and
   Plaid's starts: `PlaidAdapter` only logs at INFO on the rare `NOT_READY` status, so
   there's no per-page line to update the spinner with during Plaid's `/transactions/sync`
   replay. Confirm the spinner doesn't look broken/hung during that phase (e.g. it should
   simply hold the last text, or read as still "working," not as stalled).
5. Confirm the CLI stays responsive for the whole walk and prints the final
   `Synced: ...` summary the moment the request returns — no hang after the last log line.
6. Redirect output to a file (`uv run moneta sync --full > out.txt`) and confirm it
   degrades cleanly with no raw ANSI/spinner escape codes polluting the file.

## Expected Result
- The spinner is legible and updates live against a real multi-window SimpleFIN walk,
  with no flicker or stale text.
- No "stuck" appearance during a Plaid fetch phase, even without per-window log lines.
- Piped/redirected output is clean text, no spinner escape-code garbage.

## Notes
- Unit coverage (`tests/test_cli.py::test_sync_in_process_progress_sink_is_registered_and_cleanly_removed`,
  `test_sync_without_setup_fails_cleanly`) only proves the loguru sink is registered and
  removed correctly, and that it doesn't crash under `CliRunner`'s non-TTY capture — none
  of that exercises `rich`'s actual live-render loop, which only behaves the way it's
  designed to in a real terminal.
- Remote mode (`MONETA_API_URL` set) never installs the progress sink
  (`in_process = not settings.api_url` in `cli/main.py`) — it shows only the bare
  `Syncing…` spinner with no window updates. Worth a quick real check against an actual
  `moneta serve` that this reduced experience is acceptable, not just inferred from the
  code.
- Design 2026-07-16 "Sync progress feedback" entry, `docs/PRD.md` §6.1.
