# Plaid unlink: /item/remove against real API, billing stops, local data kept

**Feature:** Plaid integration — `moneta setup plaid-unlink` (`src/moneta/cli/main.py`, `remove_item` in `src/moneta/aggregator/plaid.py`)
**Priority:** medium
**Type:** functional

## Prerequisites
- At least one linked Plaid item (sandbox is fine; production verifies billing for real)
- Access to the Plaid dashboard to confirm item status
- Previously synced data from that item in the local db

## Test Steps
1. `uv run moneta setup plaid-list` — note the item id to remove.
2. `uv run moneta setup plaid-unlink <item-id>` — confirm `[green]Unlinked <institution>.[/green]`.
3. Check the Plaid dashboard: the item no longer appears among active items (billing for it has stopped).
4. Inspect `<config_dir>/plaid_items.json`: the entry is gone; other items untouched; file still mode `0600`.
5. `uv run moneta accounts` and `moneta power` — previously synced accounts/transactions from that institution are still present (unlink never deletes db rows).
6. `uv run moneta sync` — completes without referencing the removed item.
7. Dead-item fallback: unlink an item whose token is already invalid (e.g. re-run unlink after manually restoring the old json entry, or remove the item on Plaid's side first). Expect the yellow warning `Plaid /item/remove failed (...); removing locally.` and the local entry removed anyway.
8. Negative path: `uv run moneta setup plaid-unlink bogus-id` — red error naming the id and pointing at `plaid-list`, exit code 1, json file untouched.

## Expected Result
- The item is deactivated on Plaid's side and removed from the local store; synced history remains queryable.
- A failure from Plaid's API degrades to a warning and still removes the item locally (local store is the source of truth).

## Notes
- Production billing confirmation may take a cycle to reflect on the dashboard; the immediate check is that the item disappears from the active-items list.
- Access tokens for removed items are permanently invalid — re-linking the same bank later creates a fresh item with full 730-day replay, which ingest dedup must absorb against the retained rows.
