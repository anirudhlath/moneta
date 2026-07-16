# `_closest_cadence` snaps to a cadence with no tolerance check

## Summary
When a forced-recurring group (human or LLM answered "yes, this is a bill")
has no clean cadence run, `_closest_cadence` picks whichever cadence is
nearest the **median gap** — with no tolerance requirement. A median that
falls between two buckets is silently assigned to one of them, and `power`
then multiplies the per-cycle amount by that cadence's monthly factor,
corrupting the flagship number.

## Context
Real occurrence (2026-07-15 QA session): Lifetime Fitness bills monthly on
the 1st at $265.72, but the initial prorated charge landed May 20, making the
gaps [12, 30]. `_match_cadence` found no ≥3-occurrence run (only Jun 1 + Jul 1
were clean), the LLM verification ledger forced the group in, and
`_closest_cadence(median=21)` chose biweekly (|21−14| < |21−30|). Power then
showed $265.72 × 26/12 = **$575.73/mo for a $265.72/mo gym** — fixed costs
overstated by $310.01/mo until the Aug 1 charge self-heals the run.

The forced path must pick *some* cadence, but "nearest bucket to a
between-buckets median" is the worst available signal: the newest gap
(30 days here) was exactly right.

## Acceptance criteria
- The forced-cadence fallback prefers evidence from the **newest** gaps
  (e.g. nearest cadence to the last gap, or to the median of the newest 2–3
  gaps) rather than the all-history median, and/or requires the chosen
  cadence to be within `_TOLERANCE` of the gap statistic it used — with a
  documented fallback (e.g. monthly) when nothing fits.
- Regression test: charges on day 0, +12, +42 (prorated signup then monthly)
  with a forced-recurring answer produce a **monthly** series, not biweekly.
- Existing behavior unchanged for groups where `_match_cadence` finds a run.
