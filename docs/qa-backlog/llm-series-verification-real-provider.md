# LLM verification of detected series with a real provider

**Feature:** `verify_series` (`src/moneta/pipelines/review.py`) against a real LLM
**Priority:** high
**Type:** integration

## Prerequisites
- `MONETA_LLM_MODEL` set to a real provider model with valid credentials (see `llm-classification-real-provider.md` for setup; that item covers merchant/transfer/inline-recurring prompts — this one covers only the new `_VERIFY_PROMPT`).
- Real synced history whose deterministic detection produces both true bills (streaming, insurance, rent, paycheck) and habitual-spend series (weekly grocery runs, same-station gas fill-ups, regular restaurant visits) — check `uv run moneta recurring` first to know what exists.

## Test Steps
1. On a DB that already has active series from before this feature, run `uv run moneta sync` once and note the new sync line `LLM verified N series; flagged M for review.` — N+M should equal the number of active series that had no prior `recurring_cluster` review item (first-sync backfill verifies everything at once; count the LLM calls/cost).
2. Cross-check wave-throughs: every series the LLM verified silently should be a genuine bill/subscription/income stream. A grocery, gas, or dining series waved through is a quality failure (it stays in fixed costs and understates spending power).
3. Cross-check flags: `uv run moneta review` should show open `recurring_cluster` items for the habit-like series; confirm the flagged series still appear in `moneta recurring` as active and still count in `moneta power` fixed costs until answered (the LLM must never end a series itself).
4. Run `uv run moneta sync` again immediately — the verify line should report 0/0 (or be absent): no series is re-asked once a `recurring_cluster` item (open or resolved) exists for its (merchant, direction).
5. JSON adherence: watch logs for `LLM classification failed` warnings during step 1. A model that wraps `{"is_recurring": ..., "confident": ...}` in prose or returns extra keys should still parse (first-JSON-object extraction); a hard failure must flag the series (open item), never crash the sync or silently verify.
6. Audit trail: confirm verified series produced *resolved* `recurring_cluster` items with `resolution.resolved_by = "llm"` in the DB, and flagged items carry `llm_flagged: true` in payload — and that a later sync's auto-review pass leaves the flagged items untouched (they are human-only).

## Expected Result
Real model waves true bills/paychecks through silently and flags rhythm-only habits for human review; flagged series keep counting in fixed costs until a human rules; nothing is asked twice; provider errors degrade to flagged items, never crashed syncs or silent verifications.

## Notes
- `confident_yes` (`src/moneta/llm.py`) requires `is_recurring: true` AND `confident: true` — any other shape (including `None` from a parse failure) flags. The failure direction is deliberately the safe one: understated spending power.
- First sync after upgrading is the expensive one — one call per pre-existing active series. Series whose merchant was already resolved by a human (or the old inline unstable-amounts path) are skipped via the existing review ledger; verify that skip works on a real DB with prior answers.
- Design doc: `docs/superpowers/specs/2026-07-09-llm-recurring-verification-design.md` §3.1, §5.
