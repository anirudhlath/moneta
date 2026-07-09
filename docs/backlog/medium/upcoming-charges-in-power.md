# Show upcoming fixed charges for the rest of the month in `moneta power`

## Summary
`RecurringSeries.next_expected_on` is already stored, but `moneta power` gives
no hint which fixed costs haven't hit yet this month. Add an "upcoming this
month" section so "Remaining" reads correctly before rent lands.

## Context / motivation
Remaining is computed against *monthly* fixed costs, but cash reality is
lumpy: seeing "Remaining $1,800" on the 10th means something very different
if a $1,400 rent charge is due on the 15th. Listing the active outflow series
whose `next_expected_on` falls between today and month-end closes that gap.

## Acceptance criteria
- `PowerReport` gains an `upcoming` list: merchant, expected date, amount, for
  active outflow series (credit-payment series excluded, same filter as fixed
  costs) with `next_expected_on` in `(today, end of month]`.
- `moneta power` renders them under the table, e.g.
  "Upcoming this month: Rent $1,400 (Jul 15), Netflix $15.99 (Jul 28)".
- Empty list renders nothing (no noise).
- Pure read — derived from existing series state, nothing stored.
