# Full-history sync pulls deep history from the real SimpleFIN bridge

**Feature:** full-history sync (`moneta sync` first run / `moneta sync --full`)
**Priority:** critical
**Type:** integration

## Prerequisites
- Real SimpleFIN bridge connected (`moneta setup simplefin <token>` done).
- A backup copy of `~/.config/moneta/moneta.db`.

## Test Steps
1. `uv run moneta sync --full`.
2. Confirm no "date range … was capped" warnings in the output.
3. Check the oldest stored transaction and total count.
4. `uv run moneta sync` (plain re-sync) — should be a single-window request
   (fast) and pull only the overlap.
5. Sanity-check `moneta recurring` / `moneta power` on the deepened history.

## Expected Result
- Transactions reach as far back as the bridge holds. For this account set
  (verified read-only 2026-07-09 with the windowed adapter): 978 txns spanning
  2026-01-10 → 2026-07-09 across 23 accounts, zero duplicates, zero bridge
  warnings — vs 774 txns / 90 days from the old single capped request.
- No missed-event spam and no dead series resurrected as active afterwards
  (see `stale-series-real-history.md`).

## Notes
- Root cause history: the beta bridge hard-caps any request to the trailing
  90 days of the range and recommends ≤45 days ("in the future, this may be
  capped"). The adapter now walks ≤45-day windows backward from today to
  `since`, stopping after ~1 year of consecutive empty windows. The earlier
  falsy-zero (`start-date=0`) suspicion was disproven — the bridge accepts
  the epoch timestamp fine; the cap was the real issue.
- The bridge only holds what institutions provided at link time (~6 months
  here); history accumulates going forward as the bridge keeps syncing, and
  normal re-syncs capture it — `--full` stays reserved for newly linked
  accounts.
