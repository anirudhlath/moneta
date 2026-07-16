# Loan payments must derive from per-account transfer links, not merchant strings

## Summary
Banks collapse payments to *different* loan/financing accounts into one or
two descriptor strings — real data shows `"Synchrony Bank Payment Web Id"`
covering three different cards' monthly payments and
`"Synchrony Bank Payment Ppd Id"` covering others. Recurring detection
groups by normalized merchant, so it either forms one series whose
expected amount is a **median across different obligations**, or forms
nothing (interleaved payments look like sub-weekly habitual spend and are
silently skipped). `obligations` then attaches that mixed series via
`_payment_series_id` and computes wrong months-left, and `power` fixed
costs can't represent the per-card payments.

## Context
Verified on a copy of the real DB (2026-07-15) after retyping the Synchrony
accounts to `loan`: the "Ppd" group formed one `monthly $116.72` series (a
median across cards paying $97–$128), the "Web" group (Modani $64.44 +
Guitar Center $106.43 + Musician's Friend ~$96 interleaved) formed **no
series at all**, and `obligations` showed Guitar Center with the mixed
$116.72 payment while Musician's Friend showed `?`.

The per-card identity already exists in the data: **each payment outflow is
transfer-linked to an inflow on the specific card account.** Grouped by
inflow account, the payments are clean monthly series with stable amounts.

## Acceptance criteria
- For `loan`-type accounts, the monthly payment is derived from the
  transfer-linked payments **grouped by inflow account** (cadence + amount
  stats per account), not from merchant-string grouping.
- `power` fixed costs include one line per loan account's payment (labeled
  by account name, e.g. `Guitar Center / Synchrony — payment`), replacing
  whatever merged merchant series would have claimed those txns.
- `obligations` months-left uses the per-account payment; the
  Musician's-Friend-shaped case (payments present, merchant merged) no
  longer yields `?` or another account's number.
- Regression test: two loan accounts paid from checking with identical
  descriptors but different amounts/dates produce two distinct payment
  obligations with the correct per-account amounts.

## Related
- `synchrony-financing-account-typing.md` — typing is the gate that makes
  these payments visible at all; this ticket makes the derived numbers
  correct once they are.
