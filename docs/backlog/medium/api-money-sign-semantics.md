# Decide and document per-field sign semantics for API money fields

## Summary
The cents normalization (2026-07-16) unified money *encoding* — every API
response money field is an integer `*_cents` — but the API still emits two
sign conventions: signed fields (`expected_cents`, `amount_cents`,
`net_worth_cents`, `remaining_cents`, `spending_power_cents`,
`balance_cents`) and unsigned magnitudes (`total_fixed_cents`,
`liabilities_cents`, `spent_so_far_cents`, `balance_owed_cents`,
`monthly_payment_cents`, `accrual_cents`, `cash_out_cents`,
`SeriesLine.monthly_cents`). Which is which is documented nowhere and
enforced by nothing.

## Context
Surfaced by the simplify pass on `feature/api-money-cents`. The CLI
reconciles the split inline: three `fmt_money(-x)` negations (power's
fixed-costs and spent-so-far rows, networth's liabilities row) and seven
`fmt_money(abs(x))` sites assume specific fields are magnitudes. Changing a
view to honor the storage convention (negative = outflow) end-to-end would
silently flip signs in the flagship table — only one CLI test guards it.
`--json` consumers and a future web frontend must each rediscover the split.

Deliberately out of scope for the normalization wave (its contract was
"encoding only — no value/sign/magnitude changes"); needs its own decision.

## Acceptance criteria
- One documented convention per field: either all money fields carry the
  storage sign (negative = outflow) with display-side magnitude choices made
  explicit, or magnitude fields are named to say so — decided, written into
  the API docs/CLAUDE.md, and consistently applied.
- CLI call sites stop hand-negating: a bare `fmt_money(x)` (or one named
  helper for deliberate magnitude display) per cell.
- Tests pin the chosen sign for at least each formerly-ambiguous field.
- Do this before (or with) the `--json` flag ships, so scripts never bind to
  the undocumented split.

## Related
- Superseded ticket: `normalize-api-money-representation.md` (shipped —
  covered encoding, not sign).
- `docs/backlog/low/json-output-flag.md` — sequencing dependency.
