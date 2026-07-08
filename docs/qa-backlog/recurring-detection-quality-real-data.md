# Recurring detection quality on real data

**Feature:** Recurring detection pipeline (`src/moneta/pipelines/recurring.py`)
**Priority:** high
**Type:** functional

## Prerequisites
- 2-3+ months of real synced transaction history (design doc §10 risk 3: cadence detection needs ~2-3 months; SimpleFIN typically provides 90+ days on first sync), including real subscriptions and a real paycheck.

## Test Steps
1. `uv run moneta sync` with several months of real history.
2. `uv run moneta recurring` — verify every known real subscription (streaming, gym, insurance, etc.) appears with the correct cadence and expected amount.
3. Verify the real paycheck appears as an inflow `RecurringSeries` with the correct amount and cadence (e.g. biweekly).
4. Count false positives: non-recurring merchants grouped as recurring purely by coincidence (≥3 occurrences that happen to match a cadence window).
5. Count false negatives: known real subscriptions that were NOT detected — check whether merchant normalization split one merchant into multiple variant names (breaking the ≥3-occurrence grouping) or amount variance exceeded the 20% tolerance band.
6. Over subsequent months, run `uv run moneta sync` then `uv run moneta recurring --events` and confirm price-increase and missed-payment events fire accurately (e.g. against a subscription you know changed price) with no phantom "missed" events on series that are still current.
7. Check `moneta review` for `recurring_cluster` items and confirm the questions raised make sense against the real transaction history shown.

## Expected Result
Subscriptions and paychecks are reliably detected with correct cadence/amount; false-positive rate is low; price-increase and missed-payment events fire accurately over time without false alarms.

## Notes
- Design doc §1 pain point 2 — "Subscription / recurring-payment detection is unreliable... missed subscriptions, false positives, no notification when a price changes."
- Cadence detection requires ≥3 occurrences (`_MIN_OCCURRENCES`) and amount stability within 20% (`_AMOUNT_TOLERANCE`); annual subscriptions need 3 years of history to auto-detect without an LLM.
- SDD ledger residuals (`.superpowers/sdd/progress.md`): Task 15 — "ended-but-still-charging series absorbs new txns into neither fixed costs nor discretionary spend"; "`next_expected_on` never moves backward — cadence change annual→monthly delays missed events until stale date passes." Task 8 — "advance-without-event date not asserted... newest-txn tie-break nondeterministic (no secondary sort key)"; a >5% price **decrease** is also labeled `price_increase` in `EventKind` (check the event's `details` for actual direction, not just the kind). Task 7 — the LLM-accepts branch for ambiguous/irregular clusters is completely untested against a real model; this is the first real check of that path.
