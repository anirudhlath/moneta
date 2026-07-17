# Transfer dedup accuracy over a real month of data

**Feature:** Transfer dedup pipeline (`src/moneta/pipelines/transfers.py`)
**Priority:** critical
**Type:** functional

## Prerequisites
- A real synced month (or more) of data across the user's actual checking/savings/credit accounts, including at least one credit-card payment from checking, one checking↔savings transfer, and a loan/financing payment if applicable.

## Test Steps
1. `uv run moneta sync` across a full billing cycle.
2. `uv run moneta review` — resolve every `transfer_pair` item in the queue.
3. Cross-reference every real inter-account movement (CC autopay, savings transfers, loan payments) against `moneta power` / `moneta obligations` output and confirm none show up as spending or income.
4. Look for false-positive links: two unrelated same-amount transactions within the ±6-day window (`_MAX_DAYS` in `transfers.py`) incorrectly linked as a transfer (e.g. a $50 charge and an unrelated $50 refund).
5. Look for false negatives: real transfers that should have auto-linked but instead sit in the review queue, or worse, were counted as spend/income.

## Expected Result
All genuine inter-account transfers are excluded from spending/income everywhere downstream; false-positive rate is near zero; the review queue contains only genuinely ambiguous cases (e.g. two same-amount candidates in one window), not normal transfers that should have auto-linked.

## Notes
- Design doc §1 pain point 3 — "Inter-account transfers are not deduplicated" — one of the four core product pain points; a wrong dedup corrupts every downstream number including the flagship `moneta power` output.
- Auto-link threshold is confidence ≥ 0.8 (`_AUTO_LINK`); confidence is built from same-day proximity (+0.2), a transfer-keyword regex match (+0.2), and checking/savings source (+0.1) — verify these heuristics actually fire on real bank descriptors, which vary a lot by institution.
- Design 2026-07-16 §7 (unit-tested, not just this real-data pass): a greedy loser in a many-to-one collision opens a `transfer_pair` ReviewItem with its original candidates instead of vanishing, and an outflow that started ambiguous (2+ candidates) never late-auto-links even if rivals' consumption leaves exactly one candidate standing — it goes through the same review/LLM path as any other multi-candidate group. Still worth confirming this reads sanely on real multi-candidate collisions rather than only the synthetic fixtures in `tests/test_transfers.py`.
