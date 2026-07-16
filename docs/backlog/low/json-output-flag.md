# Add `--json` output to every read command

## Summary
All CLI read commands (`power`, `networth`, `cashflow`, `recurring`,
`obligations`, `accounts`) render rich tables only. Add a `--json` flag that
prints the raw API response, making moneta scriptable (Raycast, menu-bar
widgets, cron checks) without a web frontend.

## Context / motivation
The CLI is a thin client over JSON endpoints, so this is nearly free: emit
the already-received payload instead of a table. It becomes much more useful
once money fields are machine-friendly — do it together with (or after)
`docs/backlog/medium/normalize-api-money-representation.md`, since today's
mix of Decimal-as-string and pre-formatted display strings is awkward for
consumers.

## Acceptance criteria
- Every read command accepts `--json` and prints the API response as JSON to
  stdout (no rich markup, suitable for piping to `jq`).
- Exit codes unchanged; errors still go to stderr as today.
- Money representation follows the normalization ticket's convention.
- One test per command asserting valid JSON parses from stdout.
