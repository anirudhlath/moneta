# Map Fidelity NetBenefits export to moneta vesting CSV

## Summary
`moneta import vesting <file>` accepts moneta's own CSV schema
(`symbol,vested_quantity,unvested_quantity`). The real NetBenefits export has a
different (undocumented-here) format; add a converter or native parser.

## Context
NetBenefits has no official API (design doc §10.1). The v1 fallback is manual CSV
import at vesting events. Writing the real parser requires an actual export file
to fix the column names/format against — do this with a fresh export in hand.

## Acceptance criteria
- `moneta import vesting <netbenefits-export.csv>` works directly on a real export
- Vested/unvested quantities land on the right `Holding` rows
- A malformed/unrecognized file produces a clear error, not silent zeros
