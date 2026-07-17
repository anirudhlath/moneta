# Visibility & power polish — design

**Date:** 2026-07-16
**Backlog tickets:**
`docs/backlog/high/transaction-drilldown-command.md`,
`docs/backlog/medium/api-money-sign-semantics.md`,
`docs/backlog/medium/power-cadence-label-ambiguity.md`,
`docs/backlog/medium/safe-to-spend-today.md`,
`docs/backlog/medium/upcoming-charges-in-power.md`,
`docs/backlog/low/json-output-flag.md`,
`docs/backlog/low/power-history.md`
**Wave:** 3 of 5. Branch `feature/visibility-power-polish` off main (post PR #9).
Owner has delegated design decisions for this wave; they are recorded here.

## 1. API money sign semantics (decided)

**Decision: keep per-field semantics — flow fields are signed, aggregate fields
are magnitudes — and make that explicit instead of implicit.** Flipping the
aggregates to storage-sign (negative totals) would churn every consumer for no
informational gain; the ticket's real demands are documentation, an end to
inline hand-negation, and pinned tests.

- **Signed (storage convention, negative = outflow):** `Transaction.amount_cents`
  (and its new `/transactions` exposure), `SeriesOut.expected_cents`,
  `AccountOut.balance_cents`, `net_worth_cents`, `spending_power_cents`,
  `remaining_cents`, `per_day_remaining_cents` (new), review-context cents.
- **Unsigned magnitudes (aggregates/labeled quantities):** `monthly_income_cents`,
  `total_fixed_cents`, `spent_so_far_cents`, `SeriesLine.monthly_cents` +
  `expected_cents` (new), `liquid_cents`, `vested_holdings_cents`,
  `liabilities_cents`, `unvested_potential_cents`, `accrual_cents`,
  `cash_out_cents`, `balance_owed_cents`, `monthly_payment_cents`,
  `upcoming[].expected_cents` (new), history rows (new).
- CLI: new helper `fmt_outflow(magnitude_cents: int) -> str` =
  `fmt_money(-magnitude_cents)` — the three hand-negation call sites (power
  fixed-costs/spent-so-far rows, networth liabilities) become semantic calls.
  `fmt_money(abs(...))` display sites stay (sign carried by an adjacent column).
- Docs: a per-field sign table in the user guide's server-mode section (with the
  cents note); one line in CLAUDE.md's money bullet.
- Tests: one test per endpoint asserting the documented sign of each money field
  against seeded data with known signs (extends the existing int-pin tests).

## 2. Transaction drill-down (`GET /transactions`, `moneta txns`)

New pure view `views/transactions.py`:

```python
class TxnRow(BaseModel):
    id: int
    posted_on: date
    account: str
    account_type: str
    merchant: str | None
    description: str
    amount_cents: int            # signed
    series: str | None           # owning series' merchant label
    series_status: str | None    # active|ended
    series_discretionary: bool | None
    link: str | None             # None | "internal" | "loan_payment" | "cc_payment"
    counted_in_spend: bool       # the power rule, materialized per row
    excluded_because: str | None # human-readable single reason when not counted
```

`transactions_report(session, start, end, account_id=None, merchant=None) ->
list[TxnRow]` — joins Account + outer-joins RecurringSeries, loads
`classified_links` once; `link` classification: inflow leg of any link →
"internal" (never counted); outflow leg → "loan_payment" if
`inflow_is_loan_like`, "cc_payment" if inflow credit, else "internal".
`counted_in_spend` replicates power's rule exactly (outflow + spend account type
+ primary currency + not linked + not tagged to an active non-discretionary
series + in range); `excluded_because` gives the first matching reason
("inflow", "transfer", "loan payment", "credit-card payment", "fixed cost
(series X)", "non-spend account", "foreign currency"). Endpoint:
`GET /transactions?start&end&account_id&merchant` (defaults: current month;
merchant is a case-insensitive substring match). Ordered newest first.

CLI `moneta txns [--month YYYY-MM | --start D --end D] [--account ID]
[--merchant NAME]`: rich table (Date, Account, Merchant, Amount, Counted,
Why-not); excluded rows rendered dim, never hidden; a footer sums counted rows
and states the power `spent_so_far` linkage. `--month` and `--start/--end` are
mutually exclusive (clean error).

## 3. Cadence label ambiguity (decided format)

`SeriesLine` gains `expected_cents: int` (per-cycle magnitude). CLI power table:
non-monthly rows render `` `$265.72 every 2 weeks ≈ $575.73/mo` `` in the amount
cell (cadence phrases: weekly → "every week", biweekly → "every 2 weeks",
annual → "every year"); monthly rows stay bare (`$X.YY`). The merchant cell
drops its `(cadence)` suffix for non-monthly rows (the phrase carries it) and
keeps `(monthly)` off entirely — cadence is now in the amount text or implied.
Income rows get the identical treatment. Derived loan-payment lines already
carry cadence + per-cycle expected via `LoanPayment` — same rendering. A CLI
test pins the biweekly format exactly.

## 4. Safe-to-spend per day

`PowerReport.per_day_remaining_cents: int` = `round(remaining_cents /
days_left)` where `days_left = (last_day_of_month - today).days + 1` (month-end
→ 1; no division by zero). Negative remaining → negative per-day (no clamp).
`PowerReport.days_left: int` also exposed (the CLI shouldn't re-derive
calendars). CLI row: `Per day (22 days left)  $60.12` right after Remaining.

## 5. Upcoming charges

`PowerReport.upcoming: list[UpcomingCharge]` — `{merchant: str, expected_on:
date, expected_cents: int (magnitude)}` for active, non-discretionary,
non-cc-payment outflow series with `next_expected_on` in `(today, month_end]`,
PLUS derived loan payments whose projected next date
(`advance_expected_on(last_paid_on, cadence)`) falls in the window (labeled
`{account} — payment`). Sorted by date. CLI: a dim block under the table,
`Upcoming this month: Rent $1,400 (Jul 15) · Netflix $15.99 (Jul 28)`; empty →
nothing rendered.

## 6. `--json` on every read command

`power`, `networth`, `cashflow`, `recurring`, `obligations`, `accounts`,
`txns`, `status` gain `--json`: print the raw API response via
`print(json.dumps(r))` to stdout (no rich markup) and return — before any table
building. Write-flag combinations (`--end`, `--set-type`, …) with `--json`
error cleanly. One test per command asserting `json.loads(stdout)` parses and
carries a known key. Error paths unchanged.

## 7. Power history

`GET /power/history?months=N` (1 ≤ N ≤ 60, default 6) → newest-first rows:

```python
class PowerMonth(BaseModel):
    month: str            # "2026-07"
    income_cents: int     # magnitude: actual inflows that month
    spend_cents: int      # magnitude: accrual spend that month (existing rules)
    net_cents: int        # signed: income - spend
```

**Semantics (decided, documented in the endpoint docstring):** past months
report *actual observed* flows, not today's series state — `spend_cents` is
`accrual_spend(month_start, month_end)`; `income_cents` is the mirror-image
inflow sum (positive txns, spend-account types, primary currency, transfer
links excluded — a new `accrual_income` beside `accrual_spend` in
views/cashflow.py). The newest row is the current partial month. CLI:
`moneta power --history N` renders a compact Month/Income/Spend/Net table
INSTEAD of the normal power view (net rendered signed via `fmt_money`).

## Out of scope

- Missed-payment events for derived loan payments (unchanged from wave 2).
- Sparkline rendering for history (table only).
- `--json` for setup/import/review commands (interactive or write paths).

## Testing & docs

Every new field lands in the int-pin + sign-pin endpoint tests; date-sensitive
tests are date-relative per the e2e anchoring convention. Docs: README command
table (`txns`, `--json`, `--history`), user-guide sections for each feature +
the sign table, PRD feature table/history + roadmap moves, delete the seven
ticket files.
