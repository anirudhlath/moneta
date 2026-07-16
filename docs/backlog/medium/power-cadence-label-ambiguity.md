# Power table pairs a cadence label with a monthlyized amount — ambiguous

## Summary
`moneta power` lists each fixed cost as e.g. `Lifetimefitness.Com -- Mn
(biweekly)  $575.73`. The amount is the **monthly-equivalent**
(`expected × 26/12`), but the `(biweekly)` label invites reading it as the
per-cycle charge. A reader cannot tell whether $575.73 is what leaves their
account every two weeks or per month.

## Context
Real confusion (2026-07-15): the owner asked whether Lifetime Fitness's
$575.73 was "the total or per cycle" — precisely this ambiguity (compounded
by a cadence misdetection, tracked separately in
`closest-cadence-needs-tolerance.md`). Monthly series are unambiguous
(factor 1.0); weekly/biweekly/annual rows all display a number that matches
neither the bank statement nor the label next to it.

## Acceptance criteria
- Non-monthly rows make both numbers explicit, e.g.
  `$265.72 every 2 weeks ≈ $575.73/mo` (exact format up to implementation),
  or the table grows a per-cycle column alongside the monthly-equivalent.
- Monthly rows stay as-is (no `≈` noise when the factor is 1.0).
- The income section gets the same treatment (biweekly paychecks have the
  identical ambiguity).
- A CLI test pins the rendering for a biweekly series.
