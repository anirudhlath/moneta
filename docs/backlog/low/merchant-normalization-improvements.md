# Merchant normalization: over-aggressive digit-stripping and direction-blind review dedup

## Summary
Two small correctness gaps in merchant handling, neither urgent enough to
block v1:

1. `_STORE_NUM` in `src/moneta/pipelines/normalize.py` strips legitimate
   3+ digit tokens out of merchant names, not just store-number suffixes.
2. Recurring-cluster review dedup (`src/moneta/pipelines/recurring.py`) is
   keyed by merchant name only, not `(merchant, direction)`, so an inflow
   and outflow series for the same merchant name share one review/force
   decision.

## Context
`_STORE_NUM = re.compile(r"(#\d+|\b\d{3,}\b)")` is meant to strip trailing
store-number tokens like `"BLUE BOTTLE #1234"` → `"Blue Bottle"`. But it
matches *any* standalone 3+ digit run, including ones that are part of the
actual merchant name — e.g. `"1-800-FLOWERS"` has `"800"` as a
hyphen-bounded (word-boundary) standalone digit run and gets mangled.

Separately, `detect_recurring`'s `reviewed`/`force` maps (see the Fix 1
work in this same review pass) are keyed by `merchant: str` alone, matching
the pre-existing `reviewed` set's original key shape. A merchant that
appears as both an outflow series (a bill) and an inflow series (e.g. a
refund/rebate program under the same display name) would collide on the
same review/force entry, potentially applying one direction's user
decision to the other.

## Acceptance criteria
- `_STORE_NUM` (or `rule_normalize` generally) stops stripping digit runs
  that are part of a known-word-adjacent merchant token — at minimum, don't
  strip a digit run immediately followed by an alphabetic token in the same
  hyphen-joined word (e.g. `1-800-FLOWERS`). Add a regression test.
- Recurring-cluster review/force keys become `(merchant, direction)`
  tuples end to end (`ReviewItem.payload`, the `reviewed`/`force` maps, and
  any consumers). Add a test with same-named inflow and outflow series
  resolved oppositely, asserting each is handled independently.
