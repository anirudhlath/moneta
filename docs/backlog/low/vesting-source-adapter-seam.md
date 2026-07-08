# Introduce a VestingSource adapter seam

## Summary
The design doc names a `VestingSource` adapter as the intended
abstraction for vesting data, but `src/moneta/vesting.py` is implemented
as plain functions (`parse_vesting_csv`, `apply_vesting`) with no
Protocol/interface. Introduce the `VestingSource` seam when a second
source is actually chosen.

## Context
Design doc §10.1 (risk 1): "Fidelity NetBenefits has no official API. v1
fallback: import the NetBenefits CSV export... Later: evaluate SimpleFIN's
Fidelity coverage for holdings and browser automation for the
vested/unvested split. **The `VestingSource` adapter isolates the
choice.**"

v1 only has one source (manual CSV import), so adding a Protocol now
would be speculative abstraction with a single implementation. This
mirrors `src/moneta/aggregator/base.py`'s `AggregatorAdapter` Protocol,
which is a reasonable pattern to follow once there's a second vesting
source to abstract over (e.g. browser automation against NetBenefits).

## Acceptance criteria
- Deferred until a second vesting data source is chosen (SimpleFIN
  holdings coverage or browser automation, per design §10.1).
- When implemented: a `VestingSource` Protocol (mirroring
  `AggregatorAdapter`) with the CSV-import path as one implementation,
  and `apply_vesting` accepting any `VestingSource` rather than raw CSV
  text directly.
