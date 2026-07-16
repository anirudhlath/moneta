# No CLI way to overrule a wrongly-confirmed recurring series

## Summary
When the LLM confidently (and wrongly) resolves "is this a recurring bill?"
as yes, the resolved ledger item forces the series in on every sync — and
there is no CLI to overrule it. `moneta review` only walks *open* items, and
`moneta recurring --end ID` doesn't stick for ongoing merchants (a fresh
untagged charge revives an ended series, and the forced-True ledger entry
keeps re-blessing it).

## Context
Real occurrence (2026-07-15): 'Aplpay Tst* Uno Mas Dallas Tx' was locked in
as a fixed cost by an LLM `is_recurring: true` resolution. The only fix was a
hand-written script: reopen the item, call
`apply_resolution(..., {"is_recurring": False}, resolved_by="manual")` —
which correctly ends the live series *and* flips the force map to suppress
the merchant forever. That capability needs a user-facing door.

## Acceptance criteria
- `moneta recurring --not-a-bill <ID>` (name negotiable) resolves the series'
  merchant/direction as not-recurring through the existing
  `apply_resolution` path: reuses the ledger item when one exists (reopening
  a resolved one), creates it when none does; ends the live series
  immediately; `resolved_by="manual"`.
- Endpoint on the API (`cli/` stays zero-logic per the architecture rule).
- The inverse mistake is also reachable: answering a review question wrong
  should be correctable (re-ask via the same command or a `--re-review ID`).
- CLI test: series with a forced-True llm ledger entry + `--not-a-bill` →
  series ended, ledger flipped manual/false, next `detect_recurring` run
  does not recreate it.

## Related
- `bills-vs-habitual-spending.md` (high) — prevents the misclassification;
  this ticket makes it correctable when it happens anyway.
- `ended-series-spend-visibility.md` — txns already tagged to the ended
  series stay excluded from power's spent-so-far for the current month.
