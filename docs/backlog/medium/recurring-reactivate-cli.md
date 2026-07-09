# Expose series reactivation in the CLI (`recurring --reactivate ID`)

## Summary
`PATCH /recurring/{id}` already accepts any `SeriesStatus` (tested with
`status=active` in `tests/test_api.py`), but the CLI only exposes
`recurring --end ID`. Ending the wrong series is currently only undoable by
hand-crafting an HTTP request.

## Context / motivation
Automatic reactivation only triggers when a *new* untagged transaction
arrives at cadence, so a mistakenly-ended series stays dead for up to a full
cadence period (a month or more) with its charges mis-bucketed. A one-flag
undo is cheap: the endpoint and its tests already exist.

## Acceptance criteria
- `moneta recurring --reactivate ID` PATCHes `{"status": "active"}` and prints
  a confirmation.
- Mutually sensible with `--end` (using both in one invocation either errors
  or applies both explicitly — pick one and test it).
- Unknown ID surfaces the API's 404 as a clean CLI error, no traceback.
- Help text on the `recurring` command mentions both flags.
