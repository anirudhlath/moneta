# LLM price-change gating on real amount drift

**Feature:** LLM-gated `expected_cents` updates in `emit_series_events` (`src/moneta/pipelines/events.py`)
**Priority:** high
**Type:** integration

## Prerequisites
- `MONETA_LLM_MODEL` set to a real provider model with valid credentials.
- Real history containing at least one series with >5% amount drift on its latest occurrence — ideally both kinds: a genuine price hike (subscription price increase) and a one-off anomaly at a bill merchant (annual true-up, add-on purchase, prorated month).

## Test Steps
1. `uv run moneta sync` over history containing a real subscription price hike. Confirm the LLM confidently confirms it: `moneta recurring` shows the new expected amount, `moneta recurring --events` shows the `price_increase` event, and no `price_change` review item was opened.
2. Sync over a one-off anomaly at a bill merchant (e.g. an annual true-up or a double charge). Confirm the LLM does NOT wave it through: `expected_cents` is unchanged, `moneta power` still uses the old amount, and `uv run moneta review` shows an open `price_change` item quoting old → new.
3. With that item still open, run `uv run moneta sync` again. Confirm no duplicate item appears and no second LLM call is made for that series (open-item suppression) — the drift is not re-asked while a question is in flight.
4. Answer the item `n` in `moneta review`, then sync again with the same anomalous amount still the latest txn. Confirm the denied (series, amount) pair stays suppressed forever — no new item, no update. Then, if a *different* drifted amount arrives later, confirm a fresh question is asked.
5. JSON adherence for `_PRICE_PROMPT`: watch logs for `LLM classification failed` during steps 1-2; a malformed/failed response must open a review item (never auto-apply, never crash).
6. Judgment quality across several real drift events: tally how often the model confirms genuine repricing vs. escalates, and whether anything anomalous got confirmed. A wrongly confirmed one-off silently corrupts `moneta power` — the exact failure this feature exists to stop.

## Expected Result
Genuine price hikes apply exactly as the pre-feature deterministic path did (event + new expected amount, no review noise); anomalies are held at the old amount behind an open `price_change` item; each (series, amount) drift costs at most one LLM call ever.

## Notes
- The money value always comes from the transaction; the LLM only answers `{"is_price_change": bool, "confident": bool}` — anything short of confident-yes queues.
- With `MONETA_LLM_MODEL` unset, drift auto-applies exactly as before this feature — worth one control run to confirm no behavior change (regression guard).
- Known pre-existing quirk (see `recurring-detection-quality-real-data.md` notes): a >5% *decrease* also emits `EventKind.price_increase`; check event `details` for direction.
- Design doc: `docs/superpowers/specs/2026-07-09-llm-recurring-verification-design.md` §3.2.
