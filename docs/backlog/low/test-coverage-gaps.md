# Consolidated test-coverage gaps from final-review triage

## Summary
A grab-bag of branches and edge cases found uncovered during the final
whole-branch review. None indicate known bugs by themselves — each is a
missing test that would catch a regression in that branch. Filed together
since they're small and scattered rather than one feature.

## Context
- **`config.py` env-precedence del-loop**: `load_settings`'s
  `for key in list(file_values): if f"MONETA_{key.upper()}" in os.environ: del file_values[key]`
  (env var wins over config-file value) has no direct test exercising the
  override with both a config file *and* an env var present for the same key.
- **SimpleFIN error/HTTP-error branches** (`aggregator/simplefin.py`):
  the `for err in data.get("errors", []): logger.warning(...)` branch, and
  `resp.raise_for_status()` raising on a non-2xx response, are both
  untested.
- **`events.py` gaps**: the "advance next_expected_on without emitting a
  missed event" path (a txn exists in the grace window — advances but
  doesn't emit); the `status != active` skip (an ended series is ignored
  entirely by `emit_series_events`); and the `s.expected_cents != 0` guard
  that avoids a division-by-zero in the price-increase drift calculation.
- **`ingest.py` gaps**: a transaction or holding whose `account_id` isn't
  in the current snapshot's accounts (`continue` branch, line ~64/~83); and
  a completely empty `Snapshot` (no accounts/transactions/holdings) through
  `ingest_snapshot`.
- **`cashflow.py::cash_out` to a loan account**: an outflow linked to a
  loan-account inflow is *not* excluded from `cash_out` (only
  liquid→liquid moves are) — this "loan payments count as cash out" branch
  has no direct test.
- **`power.py::power_report` on an empty DB**: no accounts, no series, no
  transactions — verify it returns zeroed-out values rather than raising
  (e.g. on `sum()` of an empty sequence, or a `None` division).
- **`vesting.py` duplicate-symbol CSV**: `apply_vesting` updates every
  `Holding` row matching a symbol; untested with two holdings sharing a
  symbol across different accounts, and with a CSV containing the same
  symbol on two rows (last-row-wins semantics unverified).
- **Committed parameterized-date e2e harness**: `tests/test_e2e.py` anchors
  its scenario to live `date.today()` so it always passes "today", but
  there's no committed set of fixed historical `today` values (e.g. across
  a year boundary, a leap day, month-end) to deterministically reproduce a
  date-arithmetic bug — today's harness can't be used to pin a regression
  to a specific calendar edge case.
- **`LiteLLMClassifier` error path** (`llm.py`): the `except Exception:
  logger.warning(...); return None` degrade-to-review-queue branch around
  `litellm.acompletion` has no test forcing `litellm` to raise.

## Acceptance criteria
- Each bullet above gets at least one test exercising the named branch.
- No production code changes expected unless a test uncovers an actual bug
  (in which case, split that fix out of this ticket).
