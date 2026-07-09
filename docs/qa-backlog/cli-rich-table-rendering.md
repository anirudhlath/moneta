# CLI rich table rendering in a real terminal

**Feature:** rich-table CLI output across all commands (`src/moneta/cli/main.py`)
**Priority:** medium
**Type:** functional

## Prerequisites
- A real terminal (not a captured test harness) — try both a narrow (~80-column) and a wide window.
- Real data including at least one long merchant/account name, plus a fresh/empty DB for empty-state checks.

## Test Steps
1. In a real terminal, run each of: `uv run moneta power`, `moneta networth`, `moneta cashflow`, `moneta recurring`, `moneta recurring --events`, `moneta obligations`, `moneta accounts`, `moneta review`.
2. Confirm every table renders without column truncation/wrapping that hides data, and that rich color markup (`[bold]`, `[red]`, `[yellow]`, `[green]`) is legible on both light and dark terminal themes.
3. `moneta recurring` is now 7 columns (ID, Merchant, Direction, Cadence, Expected, Next, Status) and `--events` is 5 (When, ID, Merchant, Event, Details) — with real detected series, confirm the wider tables still fit an ~80-column window without squeezing Merchant or the Details dict into unreadable wraps, and that the ID shown is directly usable as `moneta recurring --end <ID>` (and disambiguates two series sharing a merchant name, e.g. a paycheck inflow and a bill from the same org).
4. Force a long merchant/account/org name into the DB and re-check `moneta recurring`, `moneta accounts`, and `moneta power` for column overflow.
5. Bank-derived strings (merchant, account/org names, event details) are now markup-escaped in every table cell — with a real or forced name containing rich-markup characters (e.g. `ACME [USA] LLC`), confirm the brackets render literally in `power`, `recurring`, `recurring --events`, `obligations`, and `accounts` (not swallowed as a color tag, no `MarkupError`), while the deliberate styling from step 2 still works.
6. Run each command against a completely empty DB (before first sync) and confirm empty states render sensibly. In particular check `moneta recurring` and `moneta recurring --events` with zero rows — `cli/main.py`'s `recurring()` builds the `Table` only inside the `if events / else` branch, so confirm it still prints a clean empty table rather than erroring. Also check `moneta power` renders sensibly when income sources are itemized under "Income (detected)" (indented sub-rows) and when there are none.
7. Test at a very narrow width (~40 columns) to see how rich degrades under space pressure — the 7-column `recurring` table is the most likely to break here.

## Expected Result
All tables are readable with no silent data loss from truncation; empty states show a sensible "no rows" table rather than crashing or printing nothing.

## Notes
- No automated test ever renders these tables to a real TTY — `tests/test_cli.py` uses `typer.testing.CliRunner`, which captures output as plain text, not a rendered terminal. This is purely a human-judgment/visual check.
- `tests/test_cli.py::test_tables_survive_markup_hostile_merchant_names` proves a bracket-laden merchant doesn't crash the command, but only a real terminal shows whether the escaped text *renders* correctly alongside the intentional markup (step 5).
- The events Merchant column comes from an API outer join; an orphaned event (series row deleted, SQLite doesn't enforce FKs) renders as `series <id>` — if you see that on real data, it signals a dangling `SeriesEvent`, not a rendering bug.
