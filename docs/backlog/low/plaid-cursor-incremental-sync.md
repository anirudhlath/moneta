# Plaid: cursor-based incremental /transactions/sync

## Summary
PlaidAdapter replays full history every sync. Persist per-item `next_cursor` and resume.

## Context
v1 chose stateless replay (design spec 2026-07-09 §3): dedup absorbs overlap and history
is bounded at 730 days. Cursors must only advance after ingest commits, which the
`fetch() -> Snapshot` protocol can't express today.

## Acceptance criteria
- Cursor stored per item, advanced only after `ingest_snapshot` commits.
- `moneta sync --full` resets cursors.
- Mutation-during-pagination restarts from the last committed cursor.
