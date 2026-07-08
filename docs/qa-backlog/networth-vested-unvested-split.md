# Net worth vested/unvested split with real Fidelity holdings

**Feature:** Net worth view (`src/moneta/views/networth.py`) after a real vesting import
**Priority:** high
**Type:** e2e

## Prerequisites
- A real Fidelity brokerage account synced via SimpleFIN (`Holding` rows present with `quantity`/`market_value_cents`).
- A completed real NetBenefits CSV import (see `fidelity-netbenefits-csv-import.md`).

## Test Steps
1. `uv run moneta sync` to pull real Fidelity holdings.
2. `uv run moneta networth` **before** importing vesting data — confirm holdings with no vesting data are excluded from `vested_holdings` rather than counted as fully vested.
3. `uv run moneta import vesting <converted-file.csv>` with real vested/unvested quantities per symbol.
4. `uv run moneta networth` again — confirm `vested_holdings` reflects only the vested portion's market value, `unvested_potential` shows the unvested portion separately, and `net_worth` does NOT include the unvested value.
5. Cross-check the vested dollar amount against the real Fidelity NetBenefits statement's "vested value" figure.
6. Data-quality edge case: if `vested_quantity` from the CSV ever exceeds the real `quantity` on the synced `Holding` (e.g. a stale export vs. a newer sync), confirm whether net worth ends up overstated.

## Expected Result
Net worth counts only vested share value — matching design doc §1 pain point 1 ("Net worth counts unvested RSUs... including them inflates net worth and makes the number useless for decisions"). The CSV importer must correctly split real Fidelity holdings into vested (counted) and unvested (shown separately, never summed).

## Notes
- Design doc §1 pain point 1 is the product's founding complaint about incumbent apps; this test verifies it against a real Fidelity account rather than fixture holdings.
- SDD ledger Task 11 residual: "networth vested_frac unclamped — vested_quantity > quantity would overstate net worth (data-quality guard worth adding)" — explicitly exercise this with a real, possibly-stale export.
- Also Task 11: "round() banker's rounding on float products in networth fractions" — expect possible off-by-a-cent discrepancies vs. hand calculation; not itself a bug.
- Depends on `fidelity-netbenefits-csv-import.md` landing first (the real column mapping must be known before this test can run end-to-end).
