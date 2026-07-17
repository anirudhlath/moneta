# `moneta txns` exclusion reasons reconcile against a real month

**Feature:** Transaction drill-down (`uv run moneta txns`, `src/moneta/views/transactions.py`, `GET /transactions`)
**Priority:** high
**Type:** e2e

## Prerequisites
- A real month of synced data spanning several account types: checking, savings, at least one credit card, and (if available) a loan or financing-mode account — plus at least one internal transfer, one credit-card payment, and a few transactions tagged to active recurring series (both a fixed-cost bill and, if any, a discretionary habit).
- Transfer dedup and recurring detection verified first (`transfer-dedup-accuracy-real-data.md`, `recurring-detection-quality-real-data.md`) — `excluded_because` is derived from `classified_links` and series tagging, so a bad link or a wrong series tag surfaces here as a wrong reason.

## Test Steps
1. `uv run moneta sync`, then `uv run moneta txns` (current month, no filters).
2. For every dimmed (excluded) row, check the printed reason against what you know actually happened to that transaction:
   - `transfer` rows are genuinely internal moves (not a real purchase that happens to look like one).
   - `loan payment` / `credit-card payment` rows are genuinely the outflow leg paying that specific loan/card (not a misattributed sibling account sharing a similar descriptor).
   - `fixed cost (series X)` rows genuinely belong to series X and are a real recurring bill, not a coincidental one-off charge that got tagged.
   - `non-spend account` / `foreign currency` rows are on an account whose real type/currency actually warrants exclusion (check against `moneta accounts`).
3. Confirm every non-dimmed (counted) row is actually a real purchase you'd want counted as spend — nothing that should have been excluded slipped through uncounted... er, uncaught.
4. Reconcile the footer: "Counted as spend" should equal your own manual sum of the counted rows; when the range includes today, the second "Through today (power's spent-so-far)" line should exactly match `uv run moneta power`'s "Spent so far" for the same period — run both back to back and diff them.
5. Try `--account <ID>` and `--merchant NAME` against transactions you can identify by eye; confirm the filter narrows to exactly the expected rows, nothing mislabeled or dropped.
6. Repeat for a past month with `--month YYYY-MM` (no "through today" line should print) and confirm the counted total is stable across repeated syncs.

## Expected Result
- Every excluded row's reason is verifiably true against real bank/card statements and account setup — not just internally consistent with the deterministic precedence rule, but semantically correct for that specific real transaction.
- No counted row should have been excluded, and no excluded row should have been counted.
- The "through today" footer matches `moneta power`'s spent-so-far exactly for an identical range.

## Notes
- The precedence order (inflow > loan payment > cc payment > transfer > active non-discretionary fixed cost > non-spend account > foreign currency) and the footer math are fully pinned by `tests/test_transactions.py` and `tests/test_cli.py`'s `test_txns_*` tests against synthetic fixtures — this ticket exists because no synthetic fixture can prove the *first-matching* reason is the semantically correct one for a real merchant descriptor, a real shared-descriptor payment, or a real account whose inferred type is subtly wrong.
- If a reason reads false, first check `moneta accounts` for a wrong inferred account type before assuming the exclusion logic itself is broken (same caution as `cashflow-numbers-real-data.md`).
