# Add `--json` output to every read command

## Summary
All CLI read commands (`power`, `networth`, `cashflow`, `recurring`,
`obligations`, `accounts`) render rich tables only. Add a `--json` flag that
prints the raw API response, making moneta scriptable (Raycast, menu-bar
widgets, cron checks) without a web frontend.

## Context / motivation
The CLI is a thin client over JSON endpoints, so this is nearly free: emit
the already-received payload instead of a table. Money fields are already
machine-friendly (integer cents, `*_cents`, no Decimal-as-string or
pre-formatted display strings), so this needs no coordination with other
work.

## Acceptance criteria
- Every read command accepts `--json` and prints the API response as JSON to
  stdout (no rich markup, suitable for piping to `jq`).
- Exit codes unchanged; errors still go to stderr as today.
- Money fields are integer cents (`*_cents`), matching every other API response.
- One test per command asserting valid JSON parses from stdout.
