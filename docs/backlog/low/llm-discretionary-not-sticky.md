# LLM "habit" classification isn't sticky across runs

## Summary
When `detect_recurring`'s amount-unstable branch asks the LLM and gets back
`"habit"`, it sets `discretionary = True` for that run only. It is never
written into the force map. If the group's newest run of occurrences later
stabilizes into the ±20% amount-tolerance band, the unstable branch (and its
LLM call) is skipped entirely on a later sync, `discretionary` defaults back
to `False`, and a genuinely discretionary series silently reverts to being
counted as a fixed cost.

## Context / motivation
Human-resolved answers persist because they're recorded as a
`recurring_cluster` `ReviewItem` resolution and read back into detection's
`force` map (`(is_recurring, discretionary)` keyed by `series_key`) on every
run — see `src/moneta/pipelines/recurring.py`'s `force` map construction and
`test_force_map_habit_applies_on_update`. LLM answers from the unstable
branch have no equivalent durable record: they're read once, applied to
`series.discretionary` for the current run, and then forgotten. Nothing
about the fix in this wave (architect finding, 2026-07-16, task 16 — LLM-
declined detection items must be human-only) changes this; that fix only
stops autoreview's binary prompt from silently overriding a "habit"/
"not_recurring" outcome, it doesn't make "habit" itself durable.

Design question for whoever picks this up: should an LLM "habit" answer
write a `force` entry the same way a human answer does (i.e., functionally
promote itself to `(True, True)` in the force map), or should it instead
open a resolved `recurring_cluster` ReviewItem the way `verify_series`
records a confident "bill" — so the ledger, not a side-channel write, is the
one source of truth for both human and LLM answers? The two approaches
have different failure modes if the group's classification later flips.

## Acceptance criteria
- An LLM "habit" classification from detect_recurring's unstable branch
  survives a later sync where that same series' newest run stabilizes into
  the amount-tolerance band (no LLM call is made that run) — `discretionary`
  stays `True` instead of reverting to `False`.
- A regression test: seed an unstable group, get a scripted "habit" answer,
  confirm `discretionary is True`; then seed further occurrences that make
  the newest run stable; run `detect_recurring` again with `llm=None`;
  assert `discretionary` is still `True`.
- No change to the precedence rule that a human override always wins over
  an LLM-recorded answer.
