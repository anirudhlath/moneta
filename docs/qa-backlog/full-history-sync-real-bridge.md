# Full-history sync pulls deep history from the real SimpleFIN bridge

**Feature:** full-history sync (`moneta sync` first run / `moneta sync --full`)
**Priority:** critical
**Type:** integration

## Prerequisites
- Real SimpleFIN bridge connected (`moneta setup simplefin <token>` done).
- At least one institution known to retain more than 90 days of history.
- A backup copy of `~/.config/moneta/moneta.db` (or start from a fresh DB).

## Test Steps
1. Move the existing DB aside so the transactions table is empty (fresh first sync).
2. `uv run moneta sync` — watch for SimpleFIN errors in the log output.
3. Check the oldest transaction landed: it should be as old as the institution
   retains, not capped at ~90 days.
4. `uv run moneta sync --full` — must complete without duplicating transactions
   (same total count afterwards; dedup is on `(account_id, aggregator_id)`).
5. `uv run moneta sync` (plain re-sync) — should be fast and only pull the
   overlap window.

## Expected Result
- First sync and `--full` request `start-date` = epoch. **Critical check:** the
  adapter serializes `date(1970, 1, 1)` as `start-date=0`; a bridge that treats
  0 as falsy/absent would silently fall back to its ~1-day default window. If
  the deep pull returns only recent data, this is the first suspect — fix would
  be a one-line nudge of the epoch (e.g. 1970-01-02 → 86400).
- Institutions return whatever depth they retain; no client-side cap.
- Re-pulls never duplicate rows.

## Notes
- SimpleFIN data quality varies by institution (design §10 risk 2); note per-org
  history depth for future reference.
