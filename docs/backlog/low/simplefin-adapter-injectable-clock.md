# Make SimpleFINAdapter's window anchor injectable

## Summary
`SimpleFINAdapter.fetch` anchors its backward window walk on
`datetime.now(UTC).date()` internally. Everything else date-sensitive in the
codebase threads `today` explicitly (pipelines, `run_sync`); the adapter is now
the one consumer that isn't injectable.

## Context / motivation
Tests in `tests/test_simplefin.py` mirror the anchor with a `_utc_today()`
helper, which leaves a milliseconds-wide midnight-rollover race between the
test's capture and the adapter's own. Harmless in practice, but an injectable
anchor (constructor arg or `fetch(since, today=...)` with a protocol default)
would make the adapter fully deterministic under test and consistent with the
repo's explicit-`today` convention.

## Acceptance criteria
- Adapter window anchoring is injectable; production default remains UTC today.
- `tests/test_simplefin.py` window tests pass a fixed date and lose the
  `_utc_today()` mirroring.
- `AggregatorAdapter` protocol churn kept minimal (FakeAdapter/RecordingAdapter
  in tests shouldn't need new required params).
