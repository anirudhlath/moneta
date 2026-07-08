# SimpleFIN access URL with percent-encoded credentials

**Feature:** SimpleFIN adapter auth (`_split_auth` in `src/moneta/aggregator/simplefin.py`)
**Priority:** high
**Type:** regression

## Prerequisites
- A real SimpleFIN access URL claimed via `uv run moneta setup simplefin <token>` (or synthesize one), where the username or password segment contains a percent-encoded character (e.g. `%40`, `%2B`) as SimpleFIN bridge secrets sometimes do.

## Test Steps
1. Obtain a real claimed access URL and inspect whether its userinfo portion contains any `%XX` escapes; if not, construct a test URL such as `https://user:p%40ss@bridge.example/simplefin` to exercise the same code path directly against `SimpleFINAdapter`.
2. Run `uv run moneta sync` with `MONETA_SIMPLEFIN_ACCESS_URL` set to that URL (or call `SimpleFINAdapter(url).fetch()` directly against a real bridge).
3. Observe whether the request authenticates (200) or fails (401/403).

## Expected Result
Sync succeeds even when the access URL's credentials require percent-decoding before being sent as HTTP Basic Auth.

## Notes
- Known gap from the SDD progress ledger (`.superpowers/sdd/progress.md`, Task 3): "`simplefin.py` `_split_auth` doesn't URL-decode percent-encoded userinfo — real access URLs with encoded chars in secret would fail auth (worth fixing before real use)."
- `urlsplit` (used in `_split_auth`) does not decode percent-escapes, so if the real bridge secret contains reserved/non-ASCII characters, the value handed to `httpx`'s `auth=` tuple is still percent-encoded, producing a wrong Basic-Auth header.
- All existing tests (`tests/test_simplefin.py`) use plain `user:pass` credentials with no encoding, so this path is entirely unverified against a real bridge. May not manifest if the specific claimed secret happens to be plain alphanumeric — confirm with the actual real access URL before ruling it out.
- Fix location if reproduced: `src/moneta/aggregator/simplefin.py::_split_auth`.
