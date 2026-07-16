# Detect financing-mode credit cards from behavior, not names

## Summary
Store cards with 0% promo financing (Synchrony et al.) are genuine `credit`
accounts — but when one is being used as a financing vehicle its payments are
real fixed costs, and today they vanish: credit-typed accounts get the
credit-card-payment exclusion (purchases counted instead), and the financed
purchase is months in the past. Keyword typing can't fix this (the same
issuer serves daily-use cards), but **usage has an unmistakable
fingerprint** that needs no APR data:

> financing mode ≈ over the observed window, account activity is entirely
> (or fee-only-except) periodic payment *credits* — equal or near-equal
> amounts — against a positive owed balance, with no purchase debits.

## Context
Real data (2026-07-15), all typed `credit`, org "Synchrony - Credit Cards":

- Musician's Friend: 3 txns, all `AUTOMATIC PAYMENT` of exactly $64.44 on
  the 1st — zero purchases, balance $1,611.05.
- Sweetwater: $106.43 × 2, zero purchases. Guitar Center: $105.45/$97.00,
  zero purchases. CareCredit: $99/$96 then a $2,847.94 payoff (only debits
  are paper-statement fees).
- Modani even shows the financed purchase itself (−$3,493.24, then $3,000
  paid a week later).
- Contrast **OnePay** (same org): 39 purchases / 4 credits — daily-use.

None of these payments appear in `power` fixed costs or `obligations`.
This is the design doc's problem #4 failing on the author's actual data.

## Acceptance criteria
- A deterministic classifier over credit-typed accounts flags financing
  mode from observed behavior (no/fee-only debits + periodic equal-ish
  payment credits + owed balance). Fires a one-time review question
  ("X looks like promo financing being paid down — treat its payments as
  fixed costs?") rather than silently reclassifying; the answer is
  remembered in the ledger like other resolutions.
- A confirmed financing-mode account gets loan semantics in classification:
  its payment links count as fixed costs and it appears in `obligations`
  (with `--set-promo` arming the deferred-interest warning) — whether via
  the existing `loan` type or a dedicated flag is an implementation choice.
- Daily-use behavior (purchases dominate) never triggers the question;
  OnePay-shaped accounts stay plain `credit`. `--set-type` overrides still
  win and survive re-sync.
- Regression tests for both fingerprints using the real shapes above.
- Known limitation (document, don't solve): a hybrid card carrying both
  daily spend and a promo plan can't be split from transaction data alone;
  Plaid's liabilities product (promotional `apr_type`,
  `balance_subject_to_apr`) is the data-driven path if ever needed —
  ticket-worthy only when a real hybrid shows up.

## Related
- `loan-payments-derive-from-links.md` — once payments count, the
  per-account amounts must come from links (descriptors collapse issuers).
  Note the card-side postings themselves ($64.44 monthly on the account)
  are an even cleaner per-account payment signal.
