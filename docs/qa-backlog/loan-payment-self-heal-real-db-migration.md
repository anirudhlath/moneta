# Existing merged Synchrony payment series self-heals on the real DB

**Feature:** Loan-linked payment untagging/orphan-ending in `detect_recurring` (`src/moneta/pipelines/recurring.py`, the `loan_payment_outflows`/`untagged_series` logic) and per-account derivation in `queries.loan_payment_stats` / `views/financing.compute_obligations`
**Priority:** critical
**Type:** e2e

## Prerequisites
- The real production database, synced under the pre-branch code, with an existing merged `RecurringSeries` like "Synchrony Bank Payment" whose occurrences span **multiple real loan accounts** sharing one bank descriptor (this is the exact real shape `queries.loan_payment_stats`'s docstring calls out — "banks collapse different loans' payments into one descriptor").
- `moneta power` and `moneta obligations` output captured *before* upgrading, for comparison.

## Test Steps
1. On the real DB, before running any code from this branch: `uv run moneta power` and `uv run moneta obligations` — save the output (fixed-cost lines, total fixed costs, obligation rows).
2. Deploy this branch's code against that same real DB and run `uv run moneta sync`.
3. `uv run moneta recurring` — confirm the old merged series (e.g. "Synchrony Bank Payment") shows `status=ended`, and that its transactions are untagged (`series_id` cleared) rather than left dangling.
4. `uv run moneta obligations` — confirm each real loan account now has its own row with a plausible monthly payment/balance/months-left, derived independently even though the payments shared one bank descriptor.
5. `uv run moneta power` — compare fixed-cost lines and the total against the "before" capture: the old merged series line should be gone, replaced by per-account loan-payment lines (`views/power`'s per-loan-account payment lines), and the total fixed-cost number should reflect real per-account payments rather than one merged (possibly wrong) figure.
6. If any of the real loan accounts are actually `type=credit` financing-mode candidates (see the companion fingerprint ticket) rather than `type=loan`: confirm their self-heal is gated on answering the financing review question first — `detect_financing` runs before `detect_recurring`'s untagging can take effect for those, since `inflow_is_loan_like` needs `financing_mode=True`. Answer the question, sync again, and confirm those specific series heal on *that* run, not the first one.

## Expected Result
The transition from one merged multi-loan series to correct per-account obligations happens automatically on the first sync after upgrade, with no data loss (no orphaned tagged transactions, no double-counted or missing fixed-cost lines) and no manual DB surgery required. `moneta power`'s fixed-cost total before and after should differ only by the amount the old merged series was wrong (over- or under-counting relative to the real per-loan payments), not by noise.

## Notes
- Unit-level coverage exists in `tests/test_recurring.py` (`test_existing_merged_payment_series_ends`, `test_partial_untag_keeps_series`) and `tests/test_financing.py` (`test_merged_descriptors_two_loans_two_obligations`), but each test builds a synthetic 2-3-month, 1-2-loan scenario from scratch. The real DB has years of history, possibly more than two loans sharing a descriptor, pre-existing `ReviewItem`s and `TransferLink`s created under older code, and the `run_sync` stage ordering interaction with `detect_financing` noted in step 6 — none of that combination is exercised by the synthetic fixtures or `test_e2e.py`'s fresh-sync-from-empty-DB scenario.
- design doc: `docs/superpowers/specs/2026-07-16-detection-correctness-design.md` §3; CLAUDE.md's "Pipeline order is load-bearing" note on `run_sync`'s stage sequence is directly relevant to step 6.
