# Financing-mode fingerprint fires correctly on real Synchrony accounts

**Feature:** Deterministic financing-mode fingerprint (`src/moneta/pipelines/financing.py::detect_financing`), wired into `run_sync` after `link_transfers`, before auto-review
**Priority:** critical
**Type:** e2e

## Prerequisites
- The real SimpleFIN-linked Synchrony accounts synced at least once with ≥2 payment cycles of history each (some are already `type=loan` via keyword inference in `pipelines/ingest.py::_TYPE_HINTS` — e.g. names containing "synchrony"/"loan"/"financing"; others stay `type=credit` because their name contains "card", which matches the `credit` needle *before* the `loan` needle is even checked — those are the ones `detect_financing` actually scans).
- A non-Synchrony revolving card synced too (the "OnePay"-shaped daily-use card), to confirm the fingerprint never mistakes ordinary spend for financing.

## Test Steps
1. `uv run moneta sync` against the real bridge.
2. `uv run moneta accounts` — note which Synchrony accounts inferred as `loan` vs stayed `credit`.
3. `uv run moneta review` — for each `credit`-typed Synchrony account, confirm a "financing check" question opens (`'{name}' looks like promo financing being paid down — treat its payments as fixed costs?`). Confirm the daily-use card gets **no** question.
4. Answer each real financing question `y`/`n` per the account's actual nature; confirm `moneta accounts` then shows `credit (financing)` for the ones answered yes.
5. `uv run moneta sync` again — confirm the same accounts are **not** re-asked (the resolved `ReviewItem` is the one-time gate, keyed by `account_id`).
6. `uv run moneta obligations` — confirm every financing-mode account and every `loan`-typed account gets a row with a real monthly payment derived from its transfer-linked payments (see the companion self-heal ticket for the derivation itself).

## Expected Result
The fingerprint (owed balance + ≥2 near-equal payment credits within ±20% of the median + no significant purchase debit since the first payment, `_MINOR_FRACTION=0.25`) fires exactly on the real promo-financed cards and never on the daily-use card, across the account's actual multi-year history — not just the four synthetic shapes (Musician's Friend, CareCredit, Modani, OnePay) the regression tests encode.

## Notes
- The regression suite (`tests/test_financing_detect.py`) pins the algorithm against fixtures explicitly modeled on these real account shapes, but only as short synthetic snippets (3-4 transactions). Real accounts have years of history with statement fees, promo-rollover purchases, occasional double payments, and payoff/re-finance events the synthetic fixtures don't attempt — a false fire (asks about a card you actively use) or false miss (silently skips a real financing card) can only be seen against the real data.
- `detect_financing` only scans `Account.type == AccountType.credit`; a Synchrony account that keyword-inferred to `loan` never reaches this code path at all — its payments are already fixed costs via `loan_payment_stats` regardless of `financing_mode`. Worth confirming the type split lands the way §2 of the design doc assumes for each of the real accounts, since it depends entirely on each account's exact name string.
- design doc: `docs/superpowers/specs/2026-07-16-detection-correctness-design.md` §2.
