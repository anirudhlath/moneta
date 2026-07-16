# `moneta backup` produces a restorable snapshot of the real database

**Feature:** `moneta backup` via SQLite `VACUUM INTO` (`POST /backup` in `src/moneta/api.py`)
**Priority:** high
**Type:** functional

## Prerequisites
- Your real `~/.config/moneta/moneta.db` with a meaningful amount of history (ideally after
  a full sync).
- Enough free disk space for a second full copy of the DB.

## Test Steps
1. Note the current DB file size and a few identifying facts (row counts per table, or
   just `moneta power` / `moneta networth` output) as a baseline.
2. `uv run moneta backup` (no dest — should default to
   `moneta-backup-<timestamp>.db` next to the real DB) while the DB is **not** being
   written to. Confirm it succeeds and the file appears.
3. Run `uv run moneta backup` again *while* a `moneta sync` is in flight in another
   terminal, to confirm `VACUUM INTO` is safe against concurrent writers (it should be,
   per SQLite's docs, but verify against the real WAL-mode file, not a fresh test DB).
4. Copy the backup file to a scratch location, point `MONETA_CONFIG_DIR`'s DB path at it
   (or copy it over a scratch `moneta.db`), and run `moneta status`, `moneta power`,
   `moneta accounts`, `moneta recurring` against the restored copy.
5. Diff the restored copy's output against the baseline from step 1 — every account,
   transaction count, series, and review item should match exactly.
6. Confirm the restored file opens cleanly with `sqlite3 <backup> "PRAGMA integrity_check"`.

## Expected Result
- The backup command succeeds both idle and under concurrent write load, without
  corrupting either the source DB or the backup.
- The restored copy is a fully functional, byte-for-byte-equivalent-in-content moneta
  database — every view and command works against it identically to the original.
- `PRAGMA integrity_check` returns `ok`.
- Re-running backup to the same default-computed path within the same second (or an
  explicit `--dest` that already exists) correctly fails with a 409-style "destination
  already exists" error rather than silently overwriting.

## Notes
- `tests/test_api.py::test_backup_vacuum_into` verifies the file is created, is non-empty,
  and that a second call to the same dest 409s — all against a freshly-created, idle test
  DB with no restore step. It never actually reopens the backup as a live database, so it
  can't catch subtle `VACUUM INTO` issues (partial writes, WAL not fully checkpointed into
  the copy, foreign-key or index corruption) that only surface when you try to use the
  restored file for real.
