# Account-type inference types real Synchrony financing accounts as `credit`

## Summary
`infer_account_type` checks keyword buckets in order, and
`credit: ("credit", "card")` is tested **before**
`loan: (..., "synchrony", "financing", ...)`. Real Synchrony accounts arrive
from SimpleFIN with org `"Synchrony - Credit Cards"` — so every one of them
matches `credit` first and the `synchrony` loan keyword is unreachable. With
type `credit`, the payment transfer-links are classified as ordinary
credit-card payments: excluded from fixed costs and from recurring detection
on the theory that card *purchases* are counted instead. For deferred-
interest financing cards the purchases are months in the past — the real
~monthly payments vanish from `power` entirely. This is the design doc's
problem #4 (the app's marquee use case) failing on the author's actual data.

## Context
Real data (2026-07-15): seven Synchrony accounts, all typed `credit` — four
carrying financed balances with observed monthly payments from checking
($64.44, ~$106.43, ~$96–128, …), 11 transfer links already matched. None of
those payments appear in `power` fixed costs; `moneta obligations` shows only
the auto loan. The unit test (`infer_account_type("CarCareONE",
"Synchrony Bank") == loan`) passes only because that synthetic org lacks the
words "credit"/"card".

Note the ambiguity is real: OnePay CashRewards (43 txns) is a genuine
daily-use revolving card at the same org — blanket "synchrony → loan" would
be wrong too. Name-only inference cannot distinguish financing from
revolving use.

## Acceptance criteria
- A real-shaped Synchrony financing account (name
  `"GUITAR CENTER / SYNCHRONY BANK"`, org `"Synchrony - Credit Cards"`)
  infers as `loan` (e.g. store/financing keywords or org-specific rules take
  precedence over the generic credit/card match), OR ambiguous cases surface
  a one-time review question ("is this financing or a regular credit card?")
  instead of silently defaulting.
- A daily-use card at the same org can still be (or be corrected to)
  `credit`, and `--set-type` overrides continue to survive re-sync.
- Regression test with the real org string `"Synchrony - Credit Cards"`.

## Related
- `loan-payments-derive-from-links.md` — even correctly-typed accounts need
  per-account payment derivation before obligations math is trustworthy.
