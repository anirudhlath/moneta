# Normalize API money representation

## Summary
The API currently mixes three different money encodings across responses.
Pick one representation (recommend: cents everywhere, formatted at the
edge) and apply it consistently before a web frontend consumes this API.

## Context
Found during the moneta architecture review (task 15). Examples of the
inconsistency, all in `src/moneta/api.py` unless noted:

- **Decimal-as-JSON-string** for report models (`PowerReport.total_fixed`,
  `NetWorthReport.net_worth`, the new `CashflowReport.accrual`/`cash_out`,
  etc. in `src/moneta/views/*.py`) — Pydantic v2 serializes `Decimal` as a
  JSON string, so clients must parse a string to get a number.
- **Pre-formatted display strings** on `AccountOut.balance` (e.g.
  `f"{a.balance_cents / 100:.2f}"`) and `SeriesOut.expected_amount` — these
  are UI-ready strings baked into the API layer, not machine-usable
  numbers, and they throw away the sign (`expected_amount` uses `abs()`).
- **Raw cents ints** on `SeriesOut.expected_cents` and internally
  throughout `src/moneta/models.py` / the pipelines.

A CLI-only client can paper over this by formatting whatever it's given,
but a web frontend (design doc §11 "Later") will need to do real math and
comparisons on these values (sorting, filtering, editing), and three
encodings for the same concept makes that error-prone.

## Acceptance criteria
- One documented convention for representing money in API responses
  (recommended: integer cents on every money field; no pre-formatted
  display strings; formatting happens in the CLI/frontend, not the API).
- All response models in `src/moneta/api.py` and `src/moneta/views/*.py`
  updated to follow it.
- CLI formatting logic (`src/moneta/cli/main.py`) updated to format cents
  itself rather than relying on server-supplied strings.
- Existing tests updated to assert against the new representation; no
  behavior change to the underlying money values.
