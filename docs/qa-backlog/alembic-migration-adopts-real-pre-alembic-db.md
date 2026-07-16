# Alembic migration adopts the real pre-Alembic database without data loss

**Feature:** Alembic migrations (`init_db` stamps a pre-Alembic DB at baseline 0001, then upgrades to head)
**Priority:** critical
**Type:** migration

## Prerequisites
- A copy of your real `~/.config/moneta/moneta.db` from before this branch (a DB created
  by the old `Base.metadata.create_all` path, with no `alembic_version` table).
- This branch's build installed (`uv sync`), so `alembic`/`tomli_w` are available.
- **Back up the real DB file first** (`cp ~/.config/moneta/moneta.db ~/.config/moneta/moneta.db.bak-pre-migration`)
  before doing anything else — this test intentionally runs migrations against production data.

## Test Steps
1. Record baseline counts against the backup for comparison: row counts for `accounts`,
   `transactions`, `recurring_series`, `transfer_links`, `series_events`, `holdings`,
   `merchant_aliases`, `review_items` (e.g. `sqlite3 moneta.db.bak-pre-migration
   "select count(*) from transactions"` for each table).
2. Point `MONETA_CONFIG_DIR` (or the real config dir) at a scratch copy of the real DB, not
   the backup.
3. Run any moneta command that triggers `init_db` (e.g. `uv run moneta status` or
   `uv run moneta accounts`).
4. Inspect the DB afterwards: confirm an `alembic_version` table exists with version `0002`
   (or the current head), and a `sync_runs` table now exists.
5. Re-run the row counts from step 1 against the migrated DB and diff them against the
   pre-migration counts.
6. Run `uv run moneta power`, `uv run moneta recurring`, `uv run moneta networth` and
   confirm the numbers match what you saw before this branch (spot-check a few known
   transactions/series by id or description).

## Expected Result
- `init_db` stamps the real DB at baseline `0001` (since it has `accounts` but no
  `alembic_version`) and then upgrades cleanly to head — no exceptions, no manual
  intervention required.
- Every table's row count is unchanged after migration; no rows silently dropped or
  duplicated.
- All existing account/transaction/series/review data reads back identically through the
  CLI views.
- The new `sync_runs` table is empty (no rows) until the next `moneta sync`.

## Notes
- `tests/test_migrations.py::test_init_db_adopts_pre_migration_database` covers this
  mechanism against a synthetic in-memory DB built from `Base.metadata`. It cannot catch
  quirks specific to a real, organically-grown database file: WAL/journal files left over
  from the old code path, unexpected NULL values in columns the baseline migration assumes
  are populated, or a stale schema that drifted from `Base.metadata` in ways the synthetic
  test can't reproduce. This is the one-shot, irreversible-in-place migration real users
  will actually hit — verify it against real data before this ships.
- If anything looks wrong, restore from `moneta.db.bak-pre-migration` immediately.
