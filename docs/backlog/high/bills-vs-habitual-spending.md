# Recurring detection conflates bills with habitual spending

## Summary
`power` treats every active outflow series as a **fixed cost**, and the LLM
prompts ask "is this a recurring bill?" — but a weekly restaurant habit *is*
recurring in plain English. The LLM confidently answers yes, the force-map
ledger locks that answer in forever, and discretionary spending gets booked
as an obligation: fixed costs inflate, spent-so-far understates (tagged txns
are excluded), and the flagship number misleads in both directions.

## Context
Real occurrence (2026-07-15): 'Aplpay Tst* Uno Mas Dallas Tx' — weekly
restaurant visits ranging **$21.76–$120.35** (5.5× spread) — became a weekly
series (expected $38.86, the newest-run median) after the LLM resolved
"is_recurring: true". Power showed **+$168.39/mo of fixed costs** for what is
discretionary dining. At the time the only fix was a hand-written
`apply_resolution` call; a CLI door now exists (`moneta recurring
--not-a-bill/--habit/--re-review ID`, shipped 2026-07-16).

The signal was available and ignored: the group failed the ±20% amount-
stability check (`stable=False`), which is precisely the fingerprint of
habitual spend vs. a bill. The inline `_LLM_PROMPT`
(`src/moneta/pipelines/recurring.py`) and `_VERIFY_PROMPT`
(`src/moneta/pipelines/review.py`) both ask "recurring bill?" without
defining the boundary, and don't show the LLM the amount spread.

## Acceptance criteria
- Both prompts define the classification power actually needs: a **fixed
  obligation** (subscription, rent, insurance, loan/membership — roughly
  stable amount, consequences if unpaid) vs. **habitual discretionary
  spending** (restaurants, coffee, bars, groceries, rideshare — variable
  amounts, a choice each time). Include the amount spread (min/median/max) in
  the prompt context.
- Amount-unstable groups (failing the ±20% check) require a *stronger* bar to
  become fixed costs: LLM must answer the bill-vs-habit question, and an
  unconfident/habit answer opens a review item instead of creating a series.
- Regression test: a weekly merchant group with amounts spread >2× and a
  scripted LLM "habit" answer produces **no fixed-cost series** (review item
  or nothing), while a stable weekly subscription still detects as before.
- Consider (optional, larger): a `discretionary` flag on RecurringSeries so
  habits can still get cadence/price tracking without entering fixed costs —
  decide in design, not required for the fix.
