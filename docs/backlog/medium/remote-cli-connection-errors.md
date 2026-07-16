# Handle connection errors cleanly in the remote CLI client

## Summary
When `MONETA_API_URL` is set and the remote server is unreachable, the CLI
crashes with a raw `httpx.ConnectError` traceback instead of a clean error
message and exit code — the local in-process path (no server) is the only
one that degrades gracefully today.

## Context
`src/moneta/cli/client.py::_arequest` only handles the HTTP-error case
(`resp.status_code >= 400` — a rich `[red]Error:[/red]` message + `exit 1`).
When `settings.api_url` is set, `httpx.AsyncClient(...).request(...)` talks
to a real socket; if the server is down, restarting, or the URL is wrong,
`httpx.ConnectError` (or `httpx.ConnectTimeout`) propagates unhandled
through `request()` → `asyncio.run()` → the typer command, printing a full
Python traceback to the user. Every other CLI failure mode in this codebase
(`test_sync_without_setup_fails_cleanly`, `test_set_promo_invalid_date_fails_cleanly`)
is asserted to print a clean message with `"Traceback" not in result.output`;
this path isn't.

## QA confirmation (2026-07-15)

Reproduced during QA against a real server: killed `moneta serve` mid-session,
ran `moneta power` with `MONETA_API_URL` still set → raw
`ConnectError: All connection attempts failed` traceback, exit 1. Also
observed on the **in-process** path: `moneta sync` with an unreachable
`simplefin_access_url` prints the same raw traceback to the console (the
SyncRun row and `moneta status` handle it correctly — only the console output
is ugly). Fix should cover both: the remote client's connection failure and
the CLI rendering of adapter connection errors from sync.

## Acceptance criteria
- `cli/client.py::_arequest` catches `httpx.ConnectError` (and reasonably
  `httpx.TimeoutException`) around the request call, prints a rich
  `[red]Error:[/red] could not reach <url>` (or similar) message, and exits
  with code 1 — same shape as the existing HTTP-error branch.
- Test: point `MONETA_API_URL` at an address nothing is listening on, run
  any CLI command, assert exit code 1, a clean error message, and no
  `"Traceback"` in the output.
