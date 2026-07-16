# Real-LLM bill/habit/not-recurring classification quality

**Feature:** Three-way LLM classification prompts — `_LLM_PROMPT` in `src/moneta/pipelines/recurring.py` (detection path for amount-unstable, cadence-matched groups) and `_VERIFY_PROMPT` in `src/moneta/pipelines/review.py` (`verify_series` second opinion)
**Priority:** high
**Type:** functional

## Prerequisites
- A configured real LLM classifier (not the scripted/stub `Classifier` the test suite injects).
- Real transaction history containing the Uno Mas-shaped restaurant habit (weekly cadence, ~5.5x amount spread, e.g. $21.76-$120.35) alongside genuine bills with looser amount stability (e.g. a utility bill with seasonal swings) — the amount-unstable-but-cadence-matched case is exactly where `detect_recurring` hands the question to the LLM instead of deciding deterministically.

## Test Steps
1. `uv run moneta sync` with an LLM configured, against real history including the Uno Mas-shaped merchant and at least one real utility/variable-amount bill.
2. `uv run moneta recurring` — confirm Uno Mas (or its real equivalent) lands as `discretionary=true` (habit), not a fixed cost, and the real variable bill still lands as a non-discretionary bill.
3. `uv run moneta power` — confirm the habit's spend counts in `spent_so_far`, not `fixed_costs`, and the bill still counts as a fixed cost despite its amount swings.
4. `uv run moneta review` — for any item the LLM was unconfident or answered `habit`/`not_recurring` on a series that's actually a bill (or vice versa), confirm the shown `llm_leaning` hint is sane, then correct it with `b`/`h`/`n` and confirm the correction sticks on the next sync (force map).
5. Repeat across a few sync cycles to see how `verify_series`'s second opinion behaves on the same real series over time — confirm it doesn't flip-flop or re-flag a series the human already ruled on (`autoreview_items` must skip `llm_flagged` items; a human-resolved item must not get re-litigated).

## Expected Result
The real LLM reliably distinguishes "recurring discretionary spending" from "recurring bill" from "coincidence, not a series" on real, messy merchant data — not just the single scripted Uno Mas fixture. Misclassifications are rare enough that the review queue stays small, and the `llm_leaning` hints shown to the human are actually useful signal rather than noise.

## Notes
- The automated suite (`tests/test_recurring.py`, `tests/test_autoreview.py`) exercises every branch of the classification logic with a scripted `FakeClassifier` returning canned `bill`/`habit`/`not_recurring`/malformed answers — it proves the *plumbing* (discretionary flag propagation, force map, `llm_flagged` gating, verify's one-shot-per-series ledger) is correct, but cannot say anything about whether a real model's judgment on ambiguous real merchants (restaurants with irregular but clustered charges, seasonal bills, hybrid habit/bill merchants like a recurring grocery delivery) is actually good. Prompt quality against real data can only be assessed by an operator reading real `llm_leaning` output against their own judgment.
- design doc: `docs/superpowers/specs/2026-07-16-detection-correctness-design.md` §1 (the Uno Mas failure is the motivating real-data case, "verified on the owner's actual accounts").
