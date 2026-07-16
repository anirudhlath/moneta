# Surface per-item sync warnings in the sync report

## Summary
ITEM_LOGIN_REQUIRED (Plaid) and SimpleFIN bridge errors only reach server logs; the CLI
user never learns an institution went stale.

## Context
`PlaidAdapter.fetch` skips dead items with `logger.warning`; SimpleFIN logs error strings.
`SyncReport` could carry a `warnings: list[str]` the CLI prints after `moneta sync`.

## Acceptance criteria
- `moneta sync` prints a yellow warning naming the stale institution and the fix
  (`moneta setup plaid-link` / SimpleFIN re-claim).
- No behavior change when all sources are healthy.
