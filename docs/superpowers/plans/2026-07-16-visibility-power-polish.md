# Visibility & Power Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transaction drill-down, formalized per-field sign semantics, per-cycle cadence labels, safe-to-spend-per-day, upcoming charges, `--json` everywhere, and month-over-month power history.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-16-visibility-power-polish-design.md` — READ IT FIRST; every design decision lives there. New pure view `views/transactions.py`; additive `PowerReport` fields; one new `views/cashflow.py` function (`accrual_income`); CLI-only rendering changes elsewhere. Suite green after every task.

**Tech Stack:** FastAPI + Pydantic v2, SQLAlchemy async, typer/rich, pytest.

## Global Constraints

- Gate before every commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — all pass, output pristine.
- Money integer cents; per-field sign semantics per spec §1 (signed flow fields vs unsigned magnitudes) — new fields follow it exactly.
- Views pure reads; `cli/` zero business logic; endpoints resolve `date.today()` at request time; tests date-relative.
- Enum columns load as `str` (`==` compares). Branch `feature/visibility-power-polish` off main.
- No schema changes this wave (no migration).

---

### Task 0: Branch
- [ ] `git checkout -b feature/visibility-power-polish main`

### Task 1: `fmt_outflow` + sign-pin tests + sign table

**Files:** Modify `src/moneta/cli/main.py`, `docs/user-guide.md`, `CLAUDE.md`; Test `tests/test_cli.py`, `tests/test_api.py`.
**Interfaces:** Produces `fmt_outflow(magnitude_cents: int) -> str` (= `fmt_money(-magnitude_cents)`, docstring: "renders an unsigned outflow/liability magnitude with its display minus"). The three hand-negation sites (power Fixed costs + Spent so far rows, networth Liabilities row) switch to it.

- [ ] Failing test: `test_fmt_outflow` (`fmt_outflow(512242) == "-$5122.42"`, `fmt_outflow(0) == "$0.00"`); implement; switch the three call sites (rendered output identical — existing CLI tests are the net).
- [ ] New `tests/test_api.py::test_money_field_signs`: seed one negative-balance account, an outflow series, a spend txn; assert documented signs — e.g. `power["total_fixed_cents"] >= 0`, `power["spent_so_far_cents"] >= 0`, `power["remaining_cents"]` free-signed, `accounts[0]["balance_cents"] < 0`, `networth["liabilities_cents"] >= 0`, `recurring[0]["expected_cents"] < 0` for the outflow series.
- [ ] Docs: user-guide server-mode section gains the per-field sign table from spec §1; CLAUDE.md money bullet gains "flow fields signed, aggregate fields unsigned magnitudes — see the user-guide table".
- [ ] Gate; commit `feat(cli): fmt_outflow semantic helper; pin per-field money signs`. Delete ticket `docs/backlog/medium/api-money-sign-semantics.md` + scrub mentions in this commit.

### Task 2: Per-cycle cadence labels

**Files:** Modify `src/moneta/views/power.py`, `src/moneta/cli/main.py::power`; Test `tests/test_power.py`, `tests/test_cli.py`.
**Interfaces:** `SeriesLine` gains `expected_cents: int` (per-cycle magnitude: `abs(s.expected_cents)` for series lines, `abs(lp.expected_cents)` for loan lines). CLI amount cell for non-monthly rows: `f"{fmt_money(line['expected_cents'])} every 2 weeks ≈ {fmt_money(line['monthly_cents'])}/mo"`; phrases: weekly "every week", biweekly "every 2 weeks", annual "every year". Monthly rows: bare `fmt_money(monthly_cents)`. Merchant cell keeps today's `({cadence})` suffix ONLY for monthly rows dropped entirely — spec §3: merchant cell has no cadence suffix anymore for any row (the amount text carries non-monthly; monthly is the default reading).

- [ ] Failing tests: `test_power.py::test_series_line_carries_expected_cents` (biweekly 250000 income → line.expected_cents == 250000, monthly_cents == 541667); `test_cli.py::test_power_biweekly_renders_per_cycle_and_monthly` pins `"$2500.00 every 2 weeks ≈ $5416.67/mo"` and that a monthly row shows a bare amount.
- [ ] Implement view + CLI (`_CADENCE_PHRASE = {"weekly": "every week", "biweekly": "every 2 weeks", "annual": "every year"}` module const; income and fixed-cost loops share one row-render helper inside the command).
- [ ] Gate; commit `feat(power): per-cycle amounts beside monthly equivalents`. Delete ticket `power-cadence-label-ambiguity.md` + scrub.

### Task 3: Safe-to-spend per day

**Files:** Modify `src/moneta/views/power.py`, `src/moneta/cli/main.py::power`; Test `tests/test_power.py`, `tests/test_cli.py`.
**Interfaces:** `PowerReport` gains `days_left: int` and `per_day_remaining_cents: int` (signed). `days_left = (monthrange-derived last day - today).days + 1`; `per_day_remaining_cents = round(remaining / days_left)` — pure ints, `calendar.monthrange` for month end.

- [ ] Failing tests: mid-month anchored (today=15th of a 31-day month → days_left 17); month-end → 1, no ZeroDivision; negative remaining → negative per-day. CLI pins `Per day (17 days left)` row after Remaining.
- [ ] Implement; gate; commit `feat(power): safe-to-spend per day`. Delete ticket `safe-to-spend-today.md` + scrub.

### Task 4: Upcoming charges

**Files:** Modify `src/moneta/views/power.py`, `src/moneta/cli/main.py::power`; Test `tests/test_power.py`, `tests/test_cli.py`.
**Interfaces:** `class UpcomingCharge(BaseModel): merchant: str; expected_on: date; expected_cents: int` (magnitude). `PowerReport.upcoming: list[UpcomingCharge]` — active, non-discretionary, non-cc outflow series with `next_expected_on` in `(today, month_end]`, plus loan payments with `advance_expected_on(lp.last_paid_on, lp.cadence)` in the window (merchant = `f"{name} — payment"`; import `advance_expected_on` from `moneta.cadence`). Sorted by `expected_on`. CLI: after the table, `Upcoming this month: X $A.BB (Jul 18) · Y $C.DD (Jul 28)` in dim; nothing when empty.

- [ ] Failing tests: series inside/outside window; discretionary + cc-payment series excluded; loan payment projected date included; ordering; CLI renders the dim line, absent when empty.
- [ ] Implement; gate; commit `feat(power): upcoming charges for the rest of the month`. Delete ticket `upcoming-charges-in-power.md` + scrub.

### Task 5: Transactions view + endpoint

**Files:** Create `src/moneta/views/transactions.py`; Modify `src/moneta/api.py`; Test new `tests/test_transactions.py`.
**Interfaces:** `TxnRow` + `transactions_report(session, start, end, account_id=None, merchant=None) -> list[TxnRow]` exactly per spec §2 (read it). Endpoint `GET /transactions` with `start: date | None`, `end: date | None`, `account_id: int | None`, `merchant: str | None` (defaults current month; substring `ilike` match on merchant). Newest first (`posted_on desc, id desc`).

- [ ] Failing tests covering every `excluded_because` branch: plain counted spend; inflow ("inflow"); internal transfer both legs; loan-payment outflow ("loan payment"); cc-payment outflow ("credit-card payment"); active-series txn ("fixed cost (…)"); ended/discretionary-series txn (counted, series fields populated); non-spend account type; foreign currency; account/merchant/date filters; endpoint default month + int/sign pins.
- [ ] Implement; the counted rule MUST reproduce `views/power.py`'s spent query semantics (same exclusions, same order of precedence for the reason string as spec §2 lists them); gate; commit `feat(api): GET /transactions drill-down view`.

### Task 6: `moneta txns` CLI

**Files:** Modify `src/moneta/cli/main.py`; Test `tests/test_cli.py`.
**Interfaces:** `moneta txns [--month YYYY-MM | --start D --end D] [--account ID] [--merchant NAME]`; `--month` with `--start/--end` → clean error exit 1. Table: Date, Account, Merchant, Amount (`fmt_money`, signed), Counted (`✓` / dim reason). Excluded rows fully dim. Footer: `Counted as spend: -$X.YY (power's spent-so-far for this range)` — sum of counted rows' magnitudes via `fmt_outflow`.

- [ ] Failing tests: table renders counted + dim excluded rows; month/start-end exclusivity error; filters pass through as query params (fake `request` pattern); footer sum pins.
- [ ] Implement (`--month` expands to first/last day via `calendar.monthrange`); gate; commit `feat(cli): moneta txns drill-down`. Delete ticket `transaction-drilldown-command.md` + scrub.

### Task 7: `--json` everywhere

**Files:** Modify `src/moneta/cli/main.py` (power, networth, cashflow, recurring, obligations, accounts, txns, status); Test `tests/test_cli.py`.
**Interfaces:** each command gains `json_output: Annotated[bool, typer.Option("--json")] = False`; when set, `print(json.dumps(r))` (plain builtin print, import json) immediately after the GET and return — before table building and before any write-flag handling check errors: combining `--json` with a write flag (`recurring --end/--not-a-bill/--habit/--re-review`, `accounts --set-*`) → red error exit 1. `status --json` prints the `/sync/last` payload (`null` → `null`).

- [ ] Failing tests: one per command asserting `json.loads(result.stdout)` parses and has a known key (`"remaining_cents"`, `"net_worth_cents"`, `"accrual_cents"`, list shapes…); write-flag+`--json` error test; no rich markup in output (`"│" not in stdout`).
- [ ] Implement; gate; commit `feat(cli): --json on every read command`. Delete ticket `json-output-flag.md` + scrub.

### Task 8: Power history

**Files:** Modify `src/moneta/views/cashflow.py` (add `accrual_income` mirroring `accrual_spend` with `amount_cents > 0` and `total = sum(t.amount_cents ...)`), `src/moneta/api.py` (endpoint + `PowerMonth` response model), `src/moneta/cli/main.py::power` (`--history N`); Test `tests/test_cashflow.py`, `tests/test_api.py`, `tests/test_cli.py`.
**Interfaces:** `accrual_income(session, start, end, links=None, primary=None) -> int` (magnitude). `GET /power/history?months=N` (Query ge=1 le=60 default 6) → `list[PowerMonth]` newest first; each month window = full calendar month, newest = current partial month; `income_cents`/`spend_cents` magnitudes, `net_cents = income - spend` signed. Docstring records the actual-observed semantics (spec §7). CLI `moneta power --history 6` renders Month/Income/Spend/Net table instead of the power view (Net via `fmt_money`, Spend via `fmt_outflow`); `--history` + `--json` prints the history JSON.

- [ ] Failing tests: `accrual_income` counts inflows minus linked; multi-month date-relative fixture → correct per-month rows + ordering; bounds (months=0 → 422); CLI table + `--json`.
- [ ] Implement; gate; commit `feat(power): month-over-month history`. Delete ticket `power-history.md` + scrub.

### Task 9: Docs

**Files:** `README.md` (command table: txns, --json, --history), `docs/user-guide.md` (txns section, --json section, power additions incl. per-day/upcoming/history, reading-the-numbers updates), `docs/PRD.md` (feature table + history entries; roadmap moves).
- [ ] Verify every documented flag/answer against the code; gate; commit `docs: wave-3 features`.

### Task 10: Review gates + smoke + PR

- [ ] Architect review (whole branch) → fix Critical/Important; simplify pass → apply; final whole-branch review (most capable model) → triage.
- [ ] QA-backlog subagent; verify-skill smoke on a seeded DB (txns drill-down reasons, per-day, upcoming, --json | jq, history), then REAL-DATA smoke: backup `~/.config/moneta/moneta.db` (`.bak-pre-wave3`), run `moneta txns`, `moneta power` (per-day/upcoming/cadence labels on real numbers), `moneta power --history 6`, `--json | python -m json.tool` — no sync needed this wave (read-only features); note anything odd.
- [ ] Push, `gh pr create` (standard body + footer), merge per standing authorization, `git checkout main && git pull`.
