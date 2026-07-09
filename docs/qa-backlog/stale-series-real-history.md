# Stale-series auto-end behaves sanely on real deep history

**Feature:** recurring-series lifecycle (auto-end stale series, reactivation)
**Priority:** high
**Type:** functional

## Prerequisites
- A deep pull completed against the real bridge (see
  `full-history-sync-real-bridge.md`) with multi-year history that includes
  subscriptions you have cancelled in the past.

## Test Steps
1. `uv run moneta sync --full` on the deep dataset.
2. `uv run moneta recurring` — inspect the status column.
3. `uv run moneta recurring --events` — inspect for missed-event spam.
4. `uv run moneta power` — inspect the fixed-cost lines.
5. If a previously-cancelled subscription is later resumed in real life (or you
   can end one via `moneta recurring --end ID` and wait for its next real
   charge + sync), confirm it flips back to active on the sync after the
   charge appears.

## Expected Result
- Long-dead subscriptions (newest charge >3 cadence periods ago) appear with
  status `ended`, not `active`.
- No flood of missed events for dead series after the deep pull.
- `moneta power` fixed costs contain only currently-live subscriptions/bills;
  the total looks plausible against your real monthly obligations.
- Currently-live series with historical breaks (pauses, card reissues) are
  still detected (cadence is judged on the newest run) and their expected
  amount reflects the current price, not a median skewed by old pricing.
- A genuinely new charge on an ended series reactivates it on the next sync.

## Notes
- Weekly-cadence series auto-end after only 21 days stale — if a weekly series
  ended mid-month, its already-tagged charges vanish from that month's
  spent-so-far (known hole, ticketed in
  `docs/backlog/medium/ended-series-spend-visibility.md`).
