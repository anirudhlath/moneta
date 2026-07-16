# Bearer-token API auth works end-to-end from a genuinely remote client over the network

**Feature:** optional bearer-token API auth (`api_token` in config, `check_auth` dependency
in `src/moneta/api.py`, non-loopback bind refusal in `moneta serve`)
**Priority:** critical
**Type:** e2e

## Prerequisites
- Two machines (or a machine + a VM/container with a separate network namespace) — one to
  run `moneta serve`, one to act as the remote CLI client. A laptop + phone-hotspot VM, or
  two hosts on the same LAN, both work.
- This branch built on both.

## Test Steps
1. On the server machine, set `MONETA_API_TOKEN` (or `api_token` in `config.toml`) to a
   real secret, then `uv run moneta serve --host 0.0.0.0 --port 8300` (or bind to the
   machine's actual LAN IP).
2. Confirm the server refuses to start with `--host 0.0.0.0` if `MONETA_API_TOKEN` is
   *not* set (should exit 1 with the "refusing to bind a non-loopback host" message before
   this test even gets going — sanity-check the guard first).
3. On the client machine, set `MONETA_API_URL=http://<server-ip>:8300` and `MONETA_API_TOKEN`
   to the *wrong* value; run `uv run moneta power`. Confirm it's rejected (401) over the
   real network, not just structurally in a test harness.
4. Set the correct token on the client; re-run `uv run moneta power` and a write path
   (`uv run moneta sync`). Confirm both succeed over the real network round-trip.
5. From a third machine (or `curl` with no Authorization header) hit the server directly:
   `curl http://<server-ip>:8300/power` — confirm 401 without a token, and that the API
   doesn't leak data before the auth dependency runs.
6. Check for latency/timeout behavior — real network round-trips are slower than in-process
   ASGI; confirm the CLI's request timeout (currently 120s in `cli/client.py`) is adequate
   and errors surface cleanly instead of hanging.

## Expected Result
- The non-loopback bind guard actually stops the server from listening on a public/LAN
  interface without a token configured.
- A remote client with the correct bearer token can fully drive `moneta` (read and sync)
  over the network.
- A remote client with a missing or wrong token is rejected with 401 on every endpoint,
  with no partial/degraded access.
- No credentials or tokens appear in server-side logs (`moneta.log`) or error responses.

## Notes
- `tests/test_api.py::test_bearer_token_enforced_when_configured` and
  `tests/test_cli.py::test_serve_refuses_public_bind_without_token` /
  `test_in_process_cli_works_with_token_configured` cover the auth dependency and the CLI's
  header-attachment logic, but only via `httpx.ASGITransport` in a single process — there is
  no real TCP socket, no real network, and the bind-refusal test monkeypatches
  `uvicorn.run` so it never actually binds a port. This item exists specifically to catch
  what only shows up over a real network: actual socket binding behavior, real HTTP header
  transport, timeout/latency handling, and whether the token genuinely gates access before
  any traffic reaches an unauthenticated machine.
