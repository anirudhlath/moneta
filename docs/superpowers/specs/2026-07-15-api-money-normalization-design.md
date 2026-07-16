# API money normalization — design

**Date:** 2026-07-15
**Backlog tickets:** `docs/backlog/medium/normalize-api-money-representation.md`,
`docs/backlog/low/power-sign-rendering-inconsistency.md`
**Wave:** 1 of 5 (backlog-clearing pass) — lands first so every later wave's new
API fields are born on this convention.

## Problem

The API mixes three money encodings:

1. **Decimal fields** on report models (`PowerReport`, `NetWorthReport`,
   `CashflowReport`, `Obligation`) — Pydantic v2 serializes `Decimal` as a JSON
   *string*, so clients parse strings to get numbers.
2. **Pre-formatted display strings**: `AccountOut.balance`
   (`f"{cents/100:.2f}"`), `SeriesOut.expected_amount` (same, plus `abs()`
   destroys the sign), and the `review_context` sample payloads
   (`dollars(txn.amount_cents)`).
3. **Raw cents ints**: `SeriesOut.expected_cents` and everything internal.

Separately, the CLI renders negative money two ways in the same power table:
`-$5412.68` (spent so far — CLI prepends `-$`) vs `$-3609.52` (remaining — raw
negative value after `$`).

## Convention

**Every money field in an API response is integer cents, named `*_cents`.**
No `Decimal`, no pre-formatted strings. Sign and magnitude of every value stay
exactly as today — only the encoding changes. Formatting happens in the client
(CLI), never in the API.

## Changes

### Response models

| Model (file) | Before | After |
|---|---|---|
| `PowerReport` (views/power.py) | `monthly_income`, `total_fixed`, `spending_power`, `spent_so_far`, `remaining` — all `Decimal` | `monthly_income_cents`, `total_fixed_cents`, `spending_power_cents`, `spent_so_far_cents`, `remaining_cents` — all `int` |
| `SeriesLine` (views/power.py) | `monthly_amount: Decimal` | `monthly_cents: int` |
| `NetWorthReport` (views/networth.py) | `liquid`, `vested_holdings`, `liabilities`, `net_worth`, `unvested_potential` — `Decimal` | same names + `_cents`, `int` |
| `CashflowReport` (api.py) | `accrual`, `cash_out` — `Decimal` | `accrual_cents`, `cash_out_cents` — `int` |
| `Obligation` (views/financing.py) | `balance_owed: Decimal`, `monthly_payment: Decimal \| None` | `balance_owed_cents: int`, `monthly_payment_cents: int \| None` |
| `AccountOut` (api.py) | `balance: str` | `balance_cents: int` |
| `SeriesOut` (api.py) | `expected_cents: int` + `expected_amount: str` | `expected_cents: int` only — `expected_amount` deleted (it was `abs()`-mangled) |

### View functions

- `views/cashflow.py::accrual_spend` and `cash_out` return `int` cents instead
  of `Decimal` (drop the `from_cents` at their tails).
- `views/power.py`, `views/networth.py`, `views/financing.py` populate the new
  int fields directly; their `from_cents` calls disappear.
- If `models.from_cents` ends up with zero callers, delete it (`to_cents`
  stays — it's the input boundary for vesting/config parsing). If anything
  still uses it, it stays.

### Review context (`GET /review` → `context`)

`_sample` is the single choke point: it feeds `recurring_cluster` /
`price_change` samples directly and `transfer_pair` outflow/candidates via
`txn_summaries`. But the same context dicts are also interpolated into LLM
prompts by `autoreview_items` (`_RECURRING_PROMPT`, `_TRANSFER_PROMPT`) and
`verify_series` (`_VERIFY_PROMPT`) — prompts want `$38.86`, payloads want
cents. The split happens at the **prompt boundary**:

- `pipelines/review.py::_sample` returns
  `{"posted_on": ..., "amount_cents": txn.amount_cents}` (int, sign intact)
  instead of `"amount": dollars(...)`. `txn_summaries` inherits this.
- `price_change` context: `old_amount` / `new_amount` become
  `old_amount_cents` / `new_amount_cents` (`int | None`, raw payload cents —
  no `dollars()`).
- The three prompt-construction sites convert context dicts to human-readable
  form at interpolation time (a small helper mapping `amount_cents` →
  `dollars()` in sample lists) — prompt *text* the LLM sees stays
  dollar-formatted and materially unchanged.
- The `dollars()` helper (`models.py`) stays — it becomes prompt-only.

### CLI (`cli/main.py`)

One helper, used by every money cell:

```python
def fmt_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole}.{frac:02d}"
```

- Negative renders as **`-$36.09`** (minus before the dollar) — resolves the
  sign-inconsistency ticket by construction, since every cell goes through the
  same function. divmod keeps it exact (no float).
- Rows that today display a value with an explicit leading minus (power's
  "Fixed costs" `-$X/mo`, "Spent so far" `-$X`; networth's "Liabilities")
  pass negated cents: `fmt_money(-cents)`. Display semantics unchanged.
- `None` handling (obligations' `?` for a missing payment) stays at call
  sites; `fmt_money` takes a plain `int`.
- Review-flow prints (`_review_one` samples, old→new price) format via
  `fmt_money` from the new `*_cents` context keys.
- No thousands separators, no other rendering changes — out of scope.

### Version skew

In-process CLI mode is same-version by construction. A remote CLI
(`MONETA_API_URL`) against an older/newer server will `KeyError` on renamed
fields. Accepted for a single-user app; no `.get()` compatibility shims for
this change. The one existing tolerance (`networth`'s
`.get("foreign_accounts")`) is untouched.

## Testing

- Update every existing assertion touching renamed/re-typed fields
  (`tests/test_api.py`, `tests/test_e2e.py`, CLI tests, view tests).
- New: one test per money-bearing endpoint (`/power`, `/networth`,
  `/cashflow`, `/obligations`, `/accounts`, `/recurring`, `/review` context)
  asserting money fields arrive as JSON **ints** (`isinstance(v, int)` on the
  parsed payload) — pins the convention against `Decimal` regressions.
- New: CLI test pinning `-$` format for a negative remaining in the power
  table (the sign ticket's acceptance criterion) and asserting the two
  negative rows use the same format.

## Docs

- `docs/user-guide.md`: any API response examples updated to cents fields.
- `docs/PRD.md`: feature-history entry (API money convention: integer cents).
- Both backlog ticket files deleted when the wave ships.

## Out of scope

- Sign-convention changes to any value (magnitudes/signs stay as today).
- Thousands separators or currency-aware formatting.
- The `--json` CLI flag (wave 3, builds on this).
- LLM prompt text formatting (`dollars()` remains for prompts).
