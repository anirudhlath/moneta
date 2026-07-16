# "Not recurring" answer ends the live series immediately

**Feature:** `apply_resolution` for `recurring_cluster` with `is_recurring: false` (`src/moneta/pipelines/review.py`)
**Priority:** high
**Type:** functional

## Prerequisites
- Real DB with an active series the LLM verification pass flagged for review (e.g. a grocery/gas habit detected as recurring — see `llm-series-verification-real-provider.md`), and note of the current `moneta power` fixed-costs number.

## Test Steps
1. `uv run moneta power` and `uv run moneta recurring` — record the flagged series' expected amount and confirm it currently counts in fixed costs.
2. `uv run moneta review`, answer `n` on the flagged `recurring_cluster` item.
3. Immediately (no sync) re-run `moneta recurring` and `moneta power` — the series must show as ended and fixed costs must drop by its amount right away, not after the ~3-cadence stale sweep.
4. `uv run moneta sync` several times over following days as new charges from that merchant arrive. The group must stay suppressed: no re-detected series, no new `recurring_cluster` question (force map), and the new charges land in discretionary spend.
5. Direction scoping: pick (or construct via a merchant with both refunds and charges, e.g. Amazon) a merchant with BOTH an inflow and an outflow series. Answer `n` for one direction only and confirm the other direction's series survives — both immediately and across the next sync.
6. Contrast case: answer `y` on a different flagged item and confirm the series stays active, keeps counting, and is never asked about again on subsequent syncs.

## Expected Result
A human "not recurring" verdict takes effect in the very next `moneta power` call — series ended, fixed costs corrected, spending power up — and the merchant+direction stays suppressed forever without collateral damage to the opposite direction.

## Notes
- Before this feature, a "no" only fed the force map: the mis-detected series kept inflating fixed costs until it went stale. This test is the user-visible proof the gap is closed on real data.
- The same immediate-ending path fires for LLM resolutions too, but by design the LLM never answers confident-no into a resolution (anything but confident-yes opens a human item), so only human answers should ever end a series — verify no series was ended with `resolved_by: "llm"` in its ledger item.
- Force-map keys are now (merchant, direction) rather than merchant-only — pre-existing merchant-only answers from before this change still parse (`series_key` returns None for payloads missing direction, which drops them from the map); if the user has old resolved items, watch for previously suppressed merchants resurfacing after upgrade.
- Design doc: `docs/superpowers/specs/2026-07-09-llm-recurring-verification-design.md` §3.4.
