# moneta serve — clean startup and shutdown with lifespan-managed engine dispose

**Feature:** `moneta serve` (real uvicorn-hosted server) — `serve` command in `src/moneta/cli/main.py`, lifespan in `src/moneta/api.py`
**Priority:** high
**Type:** integration

## Prerequisites
- A terminal able to run `uv run moneta serve` and send it signals (Ctrl-C for SIGINT; `kill -TERM <pid>` for SIGTERM).
- A second terminal/client to hit the running server (`curl`, or `MONETA_API_URL=http://127.0.0.1:8300 uv run moneta <cmd>`).
- Optional: an existing synced `moneta.db` to test the "restart against a pre-initialized DB" step; a fresh config dir works for the rest.

## Test Steps
1. Run `uv run moneta serve` against a config dir with no existing DB file. Confirm it starts without error/traceback and logs indicate the DB was initialized.
2. From the second terminal, hit a few endpoints (`/networth`, `/power`, `/accounts`, e.g. via `MONETA_API_URL=... uv run moneta networth`) and confirm 200s with sane bodies while the server is running.
3. Stop the server with Ctrl-C (SIGINT). Confirm: the process exits promptly (no hang waiting on the event loop), no traceback is printed, and no aiosqlite/"unclosed connection" garbage-collector warning appears at exit — the same class of warning commit `ca61eba` fixed for the in-process CLI path, now needs separate verification for the real server's own lifespan.
4. Restart `uv run moneta serve` against that same, now-initialized DB. Confirm clean startup a second time (exercises the `alembic_version == _HEAD` fast path plus a second engine create/dispose cycle against the same file).
5. Repeat step 3 but stop the server with SIGTERM (`kill -TERM <pid>`) instead of Ctrl-C, to confirm the same clean-shutdown/dispose behavior via a different signal path (relevant for process managers/systemd/containers).
6. Optional: issue a couple of concurrent requests (e.g. two `moneta sync` calls from separate terminals against the running server) and then stop the server, to confirm the pooled engine disposes cleanly even with recently-active connections.

## Expected Result
- The server starts and serves correctly on both a fresh and a pre-existing DB.
- On shutdown via SIGINT or SIGTERM, the process exits promptly with no traceback, no hang, and no connection-leak warning — confirming the lifespan `finally: await engine.dispose()` block (`src/moneta/api.py`) behaves correctly when driven by uvicorn's real process lifecycle, not just by the in-process ASGI harness.

## Notes
- `tests/test_cli.py::test_serve_refuses_public_bind_without_token` and `test_serve_public_bind_allowed_with_token` monkeypatch `uvicorn.run` itself — no automated test ever starts a real server process or exercises uvicorn's own startup/shutdown signal handling.
- The dispose-on-shutdown code path this branch added is otherwise only exercised via the CLI's in-process route (`AsyncExitStack` + `app.router.lifespan_context` in `cli/client.py`), which every in-process CLI test already runs under the suite's pristine-output requirement — that gives good confidence in the lifespan/dispose *logic* itself, just not in real uvicorn's process-level startup/shutdown wiring, which is what this item targets.
- Likely regression symptom if this breaks: a hang on Ctrl-C, or a GC warning about an unclosed aiosqlite connection printed after the process should have exited.
