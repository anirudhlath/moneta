# Server mode: moneta serve + MONETA_API_URL remote CLI usage

**Feature:** `moneta serve` and the remote-client path in `src/moneta/cli/client.py`
**Priority:** medium
**Type:** integration

## Prerequisites
- Two terminal sessions (or a background process) on the same machine or network.

## Test Steps
1. Terminal A: `uv run moneta serve` (defaults to `127.0.0.1:8300`).
2. Terminal B: `export MONETA_API_URL=http://127.0.0.1:8300`, then run `uv run moneta power` / `sync` / `accounts` / etc. Confirm they behave identically to in-process mode and actually hit the real HTTP server (check Terminal A's uvicorn access log to confirm requests land there).
3. Stop the server (Ctrl-C in Terminal A) and, with `MONETA_API_URL` still exported in Terminal B, run `uv run moneta power` again.
4. Observe the failure mode: a clean `[red]Error:[/red] ...` message with exit code 1 (like other error paths), or a raw Python traceback.
5. Try binding with `moneta serve --host 0.0.0.0 --port 9000` and connecting via `MONETA_API_URL` from another machine on the LAN.

## Expected Result
Remote CLI mode behaves identically to in-process mode when the server is up. When the server is down, the CLI should fail clearly, not crash with a traceback.

## Notes
- SDD ledger (Task 13): "remote-mode `httpx.ConnectError` → traceback (only non-2xx handled)." `cli/client.py::_arequest` only special-cases `resp.status_code >= 400`; a connection failure raises `httpx.ConnectError` uncaught, which will surface as a raw traceback. This QA case is expected to reproduce that known bug — if confirmed, consider promoting it to `docs/backlog/`.
- Also noted (Task 13): `build_app()`'s engine is never disposed per-request under the in-process ASGI path — not directly observable via this test, but relevant if in-process vs. remote-mode CLI sessions behave differently over long runs.
