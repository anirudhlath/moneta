# Inconsistent sign rendering in `moneta power` output

## Summary
The power table renders "Spent so far" as `-$5412.68` (minus before the dollar
sign) but "Remaining" as `$-3609.52` (minus after) in the same table. Same
value class, two formats.

## Context
Observed during QA (2026-07-15) against real data. The `/power` API returns
`spent_so_far: "5412.68"` (positive) and `remaining: "-3609.52"` (negative);
the CLI prepends `-$` to spent-so-far but formats remaining as `$` + raw
negative value (`src/moneta/cli/main.py`, power table rendering). Cosmetic,
but the flagship view should render money signs one way.

## Acceptance criteria
- One consistent format for negative money everywhere in the power table
  (pick one of `-$X` / `$-X` and apply to both rows).
- A CLI test pins the chosen format for a negative remaining value.
