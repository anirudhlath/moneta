# `moneta status` and moneta.log correctly surface a real scheduled sync failure

**Feature:** SyncRun audit trail + rotating `moneta.log` + `moneta status` (`src/moneta/pipelines/run.py`,
`src/moneta/logs.py`, `moneta status` in `src/moneta/cli/main.py`)
**Priority:** high
**Type:** e2e

## Prerequisites
- A real cron/launchd (or similar) schedule invoking `moneta sync` unattended, per the
  project's intended "did last night's sync work?" use case.
- A way to force a real failure: temporarily revoke/corrupt the SimpleFIN access URL
  (`moneta setup simplefin <bad-token>` or edit `config.toml`'s `simplefin_access_url` to a
  bad value), or briefly disconnect network access during the scheduled run.

## Test Steps
1. Set up the scheduled sync (e.g. a cron entry running `moneta sync` nightly) against a
   real config dir.
2. Let one scheduled run succeed normally first — confirm `moneta status` reports success
   with sensible new-transaction/series/event counts, and `~/.config/moneta/moneta.log`
   has a corresponding "sync ok" line.
3. Break the SimpleFIN connection (bad credentials or network cut) and either wait for the
   next scheduled run or trigger one manually in the same unattended fashion (no
   interactive terminal — redirect output the way cron would).
4. After the failed run, check `moneta status` — should report the failed run with a
   readable error message, not the last successful run.
5. Check `moneta.log` — should contain the "sync failed: ..." error line, and the file
   should still be valid (not truncated by a mid-write crash).
6. Fix the credentials, let a sync succeed again, and confirm `moneta status` now shows
   the new success, and that `sync_runs` in the DB retains the failed row for history
   (not overwritten/deleted).
7. Over multiple days of scheduled runs, confirm log rotation kicks in as expected (10 MB
   rotation, 5 retained files per the `logs.py` config) rather than growing unbounded —
   this needs either a long observation window or synthetically generating enough log
   volume to trigger a rotation.

## Expected Result
- `moneta status` after a failure clearly shows `failed` with the actual exception message,
  not a stale success or a crash of the status command itself.
- `moneta.log` captures both successes and failures with enough detail to diagnose a real
  overnight failure without re-running anything.
- The failure doesn't corrupt the DB or block subsequent syncs from succeeding.
- Log rotation and retention behave as configured under real, sustained usage (not just a
  single `configure_logging` call in a test).

## Notes
- `tests/test_run.py` and `tests/test_logs.py` cover the success/failure bookkeeping and
  log-sink idempotency in isolation (in-memory DB, synthetic exceptions, a single
  `configure_logging` call). They don't exercise a real unattended cron invocation, real
  process exit-code/output redirection behavior, or log rotation over realistic volume —
  this is the "did the thing I actually rely on to tell me sync broke, actually work"
  check.
