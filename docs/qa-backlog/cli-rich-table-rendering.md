# CLI rich table rendering in a real terminal

**Feature:** rich-table CLI output across all commands (`src/moneta/cli/main.py`)
**Priority:** medium
**Type:** functional

## Prerequisites
- A real terminal (not a captured test harness) — try both a narrow (~80-column) and a wide window.
- Real data including at least one long merchant/account name, plus a fresh/empty DB for empty-state checks.

## Test Steps
1. In a real terminal, run each of: `uv run moneta power`, `moneta networth`, `moneta recurring`, `moneta recurring --events`, `moneta obligations`, `moneta accounts`, `moneta review`.
2. Confirm every table renders without column truncation/wrapping that hides data, and that rich color markup (`[bold]`, `[red]`, `[yellow]`, `[green]`) is legible on both light and dark terminal themes.
3. Force a long merchant/account/org name into the DB and re-check `moneta recurring`, `moneta accounts`, and `moneta power` for column overflow.
4. Run each command against a completely empty DB (before first sync) and confirm empty states render sensibly. In particular check `moneta recurring` and `moneta recurring --events` with zero rows — `cli/main.py`'s `recurring()` builds the `Table` only inside the `if events / else` branch, so confirm it still prints a clean empty table rather than erroring.
5. Test at a very narrow width (~40 columns) to see how rich degrades under space pressure.

## Expected Result
All tables are readable with no silent data loss from truncation; empty states show a sensible "no rows" table rather than crashing or printing nothing.

## Notes
- No automated test ever renders these tables to a real TTY — `tests/test_cli.py` uses `typer.testing.CliRunner`, which captures output as plain text, not a rendered terminal. This is purely a human-judgment/visual check.
