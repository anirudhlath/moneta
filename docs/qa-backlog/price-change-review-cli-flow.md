# price_change review flow in the interactive CLI

**Feature:** `price_change` review kind end-to-end (`src/moneta/cli/main.py::_review_one`, `/review/{id}/resolve` in `src/moneta/api.py`, `apply_resolution` in `src/moneta/pipelines/review.py`)
**Priority:** medium
**Type:** e2e

## Prerequisites
- A real DB with at least one open `price_change` review item (produced by a sync where the LLM declined to confirm a >5% drift — see `llm-price-change-gating-real-drift.md`; the existing `cli-review-queue-interactive-workflow.md` covers the other three kinds, not this one).

## Test Steps
1. `uv run moneta review` — confirm the item renders usably: recent occurrence samples (date + amount), then the `$old → $new on <date>` line, then the `Price change? [y/n]` prompt. Amounts should read as positive dollars regardless of outflow sign.
2. Answer `y`. Confirm resolution succeeds, then — *without* running another sync — check `moneta recurring` shows the new expected amount, `moneta recurring --events` shows the `price_increase` event dated to the drift occurrence, and `moneta power` reflects the new fixed cost. This answer applies at resolve time, not on the next sync.
3. On a second open item, answer `n`. Confirm no amount/event change, and that subsequent `moneta sync` runs never re-open the question for that same (series, amount) pair.
4. Press Enter (blank) on an item — it should skip and stay open; type garbage (`maybe`) — should print invalid-input, skip, stay open.
5. API contract: `POST /review/{id}/resolve` with `{"resolution": {"is_price_change": "yes"}}` (string, not bool) must 422 with `resolution.is_price_change must be a bool`; a proper bool must succeed.
6. Read the closing line after resolving — it now says "Some answers take effect on the next sync"; sanity-check the wording isn't misleading given step 2 (price answers applied immediately).

## Expected Result
A price_change item is fully resolvable through the real interactive prompt; `y` immediately reprices the series and emits the event, `n` permanently suppresses that drift, skip/invalid leave the item open, and the API rejects non-boolean resolutions.

## Notes
- `is_price_change: true` resolution routes through the same `apply_price_change` helper the sync path uses, so the event + `expected_cents` effect should be indistinguishable from an LLM-confirmed change — compare the two on real data.
- `price_change` items are opened with `llm_flagged: true` and are skipped by LLM auto-review by construction — confirm one survives multiple syncs untouched until a human answers.
- Automated tests cover this flow only with fake prompts/stubs; the real terminal rendering (rich markup, sign display for inflow series) and the live resolve round-trip are what this verifies.
