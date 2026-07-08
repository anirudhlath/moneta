# CLI review queue interactive workflow

**Feature:** `moneta review` interactive prompt loop (`src/moneta/cli/main.py::review`)
**Priority:** medium
**Type:** functional

## Prerequisites
- Real review-queue items generated from real data — at least one each of `merchant`, `transfer_pair`, and `recurring_cluster` kinds, produced via `uv run moneta sync` against messy real-world data.

## Test Steps
1. `uv run moneta sync` repeatedly (across dirty descriptors, ambiguous transfers, irregular recurring clusters) until `moneta review` shows all three kinds of open items.
2. `uv run moneta review` — for a `merchant` item, type a real merchant name at the prompt; confirm it resolves and a re-run of `moneta review` no longer shows it.
3. For a `transfer_pair` item, enter one of the listed candidate inflow IDs; confirm the pair links correctly (cross-check with `transfer-dedup-accuracy-real-data.md`) and disappears from the queue.
4. For a `transfer_pair` item, deliberately enter a non-candidate integer ID and an out-of-range ID; observe the behavior — the CLI only validates that the input parses as `int`, not that it's a listed candidate.
5. For a `recurring_cluster` item, press Enter (blank) to skip; confirm the item is left open and reappears on the next `moneta review` run.
6. Interrupt with Ctrl-C mid-prompt and confirm no partial/corrupt resolution gets written.

## Expected Result
All three review-item kinds are resolvable through the real interactive prompt; skipping leaves items open; invalid input is handled without a crash.

## Notes
- Automated tests only cover the non-integer-input skip path for `transfer_pair` (`test_review_non_integer_answer_skips_cleanly` in `tests/test_cli.py`). The happy-path resolution flows for all three kinds, and the "valid int but wrong/out-of-range candidate" case, are untested and unverified against the real `/review/{id}/resolve` endpoint contract.
