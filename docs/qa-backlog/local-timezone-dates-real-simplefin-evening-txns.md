# Local-timezone conversion lands real evening SimpleFIN transactions on the correct calendar day

**Feature:** local-timezone SimpleFIN dates (`_ts_to_date` in `src/moneta/aggregator/simplefin.py`)
**Priority:** medium
**Type:** functional

## Prerequisites
- Real SimpleFIN bridge connected.
- A machine with `TZ` set to your actual local timezone (not UTC) — e.g. `America/Los_Angeles`
  or `America/New_York`.
- Ideally, a known transaction that posted late in the evening local time (after ~5pm local,
  since that's within a few hours of the UTC date rollover) so its UTC date and local date
  differ.

## Test Steps
1. Confirm the shell's local timezone: `date` and check it's not UTC.
2. `uv run moneta sync --full` (or a plain re-sync if already synced).
3. Cross-reference a few real transactions posted in the evening (per your bank's own
   statement/app) against what `moneta` recorded as `posted_on`.
4. Pay particular attention to transactions near a DST transition date, if one is
   available in your history, and to institutions in a different US timezone than your own
   (SimpleFIN gives a Unix timestamp, not a timezone — conversion always uses the *local*
   machine's TZ, not the institution's).

## Expected Result
- Evening transactions land on the calendar day the bank itself shows (the day the user
  would recognize), not shifted forward by the old UTC-based conversion.
- No date lands on a day that doesn't match the bank's own posted-date display for that
  transaction.

## Notes
- `tests/test_simplefin.py::test_ts_to_date_uses_local_timezone` and
  `test_ts_to_date_utc` cover the pure function with a synthetic fixed timestamp and a
  monkeypatched `TZ` env var — they prove the conversion logic is correct in isolation.
  They cannot catch real-world SimpleFIN timestamp quirks (e.g. an institution that reports
  midnight-anchored timestamps regardless of when the transaction actually posted, or a
  machine running with an unexpected `TZ`/`TZDATA` setup) that only show up against live
  data.
- If `moneta serve` runs on a different machine/timezone than the CLI user (e.g. a server
  in UTC, accessed remotely), the date conversion happens server-side — the "local" day is
  the server's local day, not necessarily the user's. Worth confirming this is the intended
  behavior if you ever deploy `moneta serve` off-machine.
