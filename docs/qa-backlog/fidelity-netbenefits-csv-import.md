# Fidelity NetBenefits CSV: real export column mapping

**Feature:** Vesting import (`src/moneta/vesting.py`) against a real Fidelity NetBenefits export
**Priority:** high
**Type:** functional

## Prerequisites
- A real vesting/holdings export from Fidelity NetBenefits (CSV, XLSX, or whatever format is actually offered).
- Real Fidelity brokerage account already synced via SimpleFIN so `Holding` rows with matching `symbol` values exist.

## Test Steps
1. Log into Fidelity NetBenefits and export the vesting/holdings data.
2. Inspect the real file's headers/format and compare against moneta's expected schema: `symbol,vested_quantity,unvested_quantity` (`src/moneta/vesting.py::_EXPECTED`).
3. Attempt `uv run moneta import vesting <netbenefits-export.csv>` directly on the unmodified export.
4. Document the actual mismatch: column names, extra columns, header/title rows, date columns, multiple grants per symbol, locale-formatted numbers (e.g. `"1,234.5"`), etc.
5. Hand-convert (or write a converter for) the real export into moneta's schema and re-run the import; confirm `uv run moneta networth` reflects the correct vested/unvested quantities against the real holding(s).

## Expected Result
Import either works directly against the real export, or fails with the existing `ValueError` (`expected header ..., got ...`) rather than silently importing zeros or garbage.

## Notes
- This is the real-file follow-up to `docs/backlog/high/fidelity-netbenefits-csv-mapping.md`, whose acceptance criteria explicitly requires "an actual export file to fix the column names/format against" — update that backlog ticket with the real column mapping discovered here.
- `apply_vesting` matches purely on `symbol`; if NetBenefits exports multiple grants for the same symbol, moneta's schema has no way to represent them separately today — repeated symbols in the CSV will each overwrite the same `Holding` row's vested/unvested fields (SDD ledger Task 11: "no duplicate-symbol CSV test").
- Watch for `float()` failing on locale-formatted quantities (thousands separators) that a real export might use.
