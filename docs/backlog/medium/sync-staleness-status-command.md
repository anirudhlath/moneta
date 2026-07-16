# Surface data staleness: last-sync timestamp + `moneta status`

## Summary
Nothing records when data was last synced, so `moneta power` against 10-day-old
data looks identical to fresh data. Record a `last_synced` timestamp and
surface it in the views; add a `moneta status` command for overall health.

## Context / motivation
Views resolve `date.today()` at request time but say nothing about how old the
underlying transactions are. A stale power report silently misleads (spent
so far too low, remaining too high). There is also no single place to see
"is moneta set up and healthy": SimpleFIN configured? LLM configured? review
queue depth? account count?

## Acceptance criteria
- `run_sync` records a sync timestamp (new single-row table or key-value
  meta table; pipelines own the commit).
- `moneta power` and `moneta networth` print a dim footer when data is stale
  (e.g. older than 24h): "data as of 2026-07-07 — run moneta sync".
- `moneta status` (and `GET /status`) shows: last sync time, number of
  accounts, open review items, whether SimpleFIN and LLM are configured
  (booleans only — never echo secrets).
- Fresh DB (never synced) states that clearly rather than showing a blank.
