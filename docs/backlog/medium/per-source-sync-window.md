# Compute the re-sync window per aggregator source

## Summary
`_sync_since` (pipelines/run.py) takes the global max `posted_on` minus 7 days. With
Plaid configured, its full replay keeps that max ≈ today on every sync, so SimpleFIN's
`start-date` stays pinned at `today − 7` forever.

## Context
If a SimpleFIN institution's connection breaks for more than 7 days (sync stays green —
SimpleFIN errors are log-and-continue), the missed days fall outside the window when it
recovers and are silently never ingested. Recovery today is a manual `moneta sync --full`.
A per-source window needs source attribution on accounts (or asking each adapter for its
own newest txn) so `since` can be computed per aggregator.

## Acceptance criteria
- SimpleFIN's `since` derives only from transactions belonging to SimpleFIN accounts.
- A SimpleFIN outage longer than the overlap self-heals on the next successful sync.
- Plaid behavior unchanged (it ignores `since`).
