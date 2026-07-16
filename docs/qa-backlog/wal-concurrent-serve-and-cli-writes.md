# WAL mode holds up under real concurrent writes from `moneta serve` and the in-process CLI

**Feature:** SQLite WAL + busy_timeout (`src/moneta/db.py` connect-time PRAGMAs)
**Priority:** high
**Type:** integration

## Prerequisites
- This branch built (`uv sync`).
- Two terminal sessions pointed at the same `MONETA_CONFIG_DIR` / `moneta.db`.
- A SimpleFIN connection or fake data so `moneta sync` has something to do (a slow sync
  gives a bigger concurrency window; a large existing DB helps).

## Test Steps
1. Terminal A: `uv run moneta serve` — leave the server running and bound to loopback.
2. Terminal B: set `MONETA_API_URL=http://127.0.0.1:8300` so the CLI talks to the server
   instead of using its in-process ASGI path, then run `uv run moneta sync` repeatedly
   in a loop (or `moneta sync --full` for a long-running one) while the server is also
   mid-request.
3. Simultaneously, in Terminal C (no `MONETA_API_URL` set — forces the in-process path,
   which opens its *own* connection to the same file), run read commands (`moneta power`,
   `moneta networth`, `moneta recurring`) back-to-back while B's sync is in flight.
4. Watch for `database is locked` errors, hangs beyond the 5s busy_timeout, or corrupted
   reads (a report that doesn't match either the pre- or post-sync state).
5. After the sync finishes, confirm both the server's view (`GET /power` via curl) and the
   in-process CLI's view agree on the data.
6. Check `~/.config/moneta/moneta.db-wal` and `-shm` files exist during the run and that
   the WAL checkpoints back into the main DB file afterward (`moneta.db` file size grows
   sensibly, `-wal` doesn't grow unbounded across many syncs).

## Expected Result
- No `database is locked` errors under normal concurrent load; if the 5000ms busy_timeout
  is exceeded, the CLI should surface a clear error, not hang indefinitely or crash with a
  raw SQLite traceback.
- Readers (Terminal C) never block writers (Terminal B) and vice versa beyond the
  busy_timeout window — that's the whole point of WAL mode.
- Data is consistent across both connections after the sync completes.

## Notes
- `tests/test_db.py::test_file_backed_engine_uses_wal_and_busy_timeout` only asserts the
  PRAGMA values are set (`journal_mode=wal`, `busy_timeout=5000`) on a single connection in
  a single process. It does not exercise actual concurrent access from two independent OS
  processes racing on the same file, which is the real scenario `moneta serve` +
  in-process CLI creates and the one WAL mode is meant to survive.
