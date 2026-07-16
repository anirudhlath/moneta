# Progress feedback during sync (especially `sync --full`)

## Summary
`moneta sync --full` walks the SimpleFIN history in ≤45-day windows back
through years of data with zero output until it finishes — it looks hung.
Show progress while the fetch walks.

## Context / motivation
The windowed walk lives in `aggregator/simplefin.py` (transport policy). A
deep pull can issue dozens of sequential bridge requests; the CLI prints
nothing until the whole sync (fetch + all pipelines) completes. Users can't
tell a slow bridge from a wedged process.

## Options
- CLI-side rich spinner/status only ("syncing…") — trivial, but says nothing
  about progress through history.
- Adapter emits per-window progress (callback or loguru at INFO) and the CLI,
  when running in-process, renders it ("fetching 2024-03…"). Remote mode
  (`MONETA_API_URL`) falls back to a plain spinner.
- Streaming endpoint (SSE) so remote CLIs get progress too — most work,
  probably overkill for a single-user app now.

The middle option fits best: adapter-level progress hook with a rich
renderer in the in-process path.

## Acceptance criteria
- `moneta sync` shows at least a spinner while the request is in flight.
- In-process `sync --full` shows which window/date range is being fetched.
- No behavior change to the sync itself; progress is presentation only.
- Remote mode degrades gracefully (spinner, no crash).
