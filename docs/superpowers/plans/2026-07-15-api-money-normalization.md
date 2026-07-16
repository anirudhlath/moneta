# API Money Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every money field in an API response becomes integer cents named `*_cents`; the CLI formats via one `fmt_money` helper with `-$X.YY` negatives.

**Architecture:** Change response models group-by-group (power, networth, cashflow, obligations, accounts/series, review context), keeping the suite green after every task. Review-context payloads switch to cents while LLM prompt text stays dollar-formatted via a prompt-boundary converter. Values keep today's exact magnitudes and signs — only encoding changes.

**Tech Stack:** FastAPI + Pydantic v2, SQLAlchemy async, typer/rich CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-07-15-api-money-normalization-design.md`

## Global Constraints

- Verification gate before every commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — all four pass, output pristine (a deprecation warning is a failure).
- Money is integer cents; `Decimal` only at boundaries via `to_cents` (models.py). Never float for money.
- Sign convention: negative = outflow, positive = inflow. This plan never changes a value's sign or magnitude — encoding only.
- Views are pure reads (no commits). `cli/` stays zero business logic.
- Work on branch `feature/api-money-cents` off `main`.
- LLM prompt text must stay dollar-formatted (`$15.99`-style) — `tests/test_autoreview.py::test_verify_prompt_carries_amounts_and_dates` pins this and must keep passing UNCHANGED.

---

### Task 0: Branch

**Files:** none

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feature/api-money-cents main
```

---

### Task 1: `fmt_money` CLI helper

**Files:**
- Modify: `src/moneta/cli/main.py` (after `_parse_iso_date`, ~line 30)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `fmt_money(cents: int) -> str` in `moneta.cli.main` — `1599 → "$15.99"`, `-3609 → "-$36.09"`, `0 → "$0.00"`. Every later task's CLI rendering calls this.

- [ ] **Step 1: Write the failing test** — append to `tests/test_cli.py`:

```python
def test_fmt_money_formats_cents() -> None:
    from moneta.cli.main import fmt_money

    assert fmt_money(0) == "$0.00"
    assert fmt_money(1599) == "$15.99"
    assert fmt_money(-3609) == "-$36.09"
    assert fmt_money(-5) == "-$0.05"
    assert fmt_money(123456789) == "$1234567.89"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_fmt_money_formats_cents -v`
Expected: FAIL — `ImportError: cannot import name 'fmt_money'`

- [ ] **Step 3: Implement** — in `src/moneta/cli/main.py`, directly after `_parse_iso_date`:

```python
def fmt_money(cents: int) -> str:
    """Integer cents -> display dollars; negatives are -$X.YY (sign before the $)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole}.{frac:02d}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::test_fmt_money_formats_cents -v`
Expected: PASS

- [ ] **Step 5: Full gate + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/cli/main.py tests/test_cli.py
git commit -m "feat(cli): fmt_money helper — cents in, -\$X.YY out"
```

---

### Task 2: Review context in cents; prompts keep dollars

**Files:**
- Modify: `src/moneta/pipelines/review.py` (`_sample` ~line 62, `autoreview_items` prompt branches ~lines 250–261, `verify_series` prompt ~line 315, `price_change` context ~lines 145–151)
- Modify: `src/moneta/cli/main.py::_review_one` (~lines 265–327)
- Test: `tests/test_api.py`, `tests/test_autoreview.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `fmt_money` from Task 1.
- Produces: `GET /review` context dicts carry `amount_cents: int` (sign intact) instead of `amount: str`; `old_amount_cents`/`new_amount_cents: int | None` instead of `old_amount`/`new_amount`. New private helper `_prompt_txn(summary: dict[str, Any]) -> dict[str, Any]` in review.py converts a context dict for prompt interpolation.

- [ ] **Step 1: Update the context assertions to cents (failing first)** — in `tests/test_api.py::test_review_context_enrichment` replace lines ~321–332:

```python
    tp = next(i for i in items if i["kind"] == "transfer_pair")
    assert tp["context"]["outflow"]["amount_cents"] == -50000
    assert tp["context"]["outflow"]["description"] == "ACH TRANSFER"
    cands = tp["context"]["candidates"]
    assert [c["description"] for c in cands] == ["DEPOSIT A", "DEPOSIT B"]
    assert cands[0]["account"] == "My Savings"
    assert cands[0]["amount_cents"] == 50000
    assert cands[0]["id"] == c1.id

    rc = next(i for i in items if i["kind"] == "recurring_cluster")
    samples = rc["context"]["samples"]
    assert len(samples) == 3
    assert samples[0]["amount_cents"] == -4500  # newest first, sign intact
```

In `tests/test_api.py::test_review_resolve_price_change_validates_and_applies` replace lines ~348–349:

```python
    assert items[0]["context"]["old_amount_cents"] == -1599
    assert items[0]["context"]["new_amount_cents"] == -1899
```

In `tests/test_autoreview.py` (~lines 342–343; the seed txn is `amount_cents=-1899` on 2026-07-15) replace:

```python
    assert ctx["old_amount_cents"] == -1599 and ctx["new_amount_cents"] == -1899
    assert ctx["samples"] == [{"posted_on": "2026-07-15", "amount_cents": -1899}]
```

Do NOT touch `test_verify_prompt_carries_amounts_and_dates` — it pins dollars in prompts.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api.py::test_review_context_enrichment tests/test_api.py::test_review_resolve_price_change_validates_and_applies tests/test_autoreview.py -v`
Expected: the two test_api tests and the autoreview context test FAIL on KeyError/assert (`amount_cents` missing); prompt tests still PASS.

- [ ] **Step 3: Implement in `src/moneta/pipelines/review.py`**

Replace `_sample` and add `_prompt_txn` next to it:

```python
def _sample(txn: Transaction) -> dict[str, Any]:
    return {"posted_on": txn.posted_on.isoformat(), "amount_cents": txn.amount_cents}


def _prompt_txn(summary: dict[str, Any]) -> dict[str, Any]:
    """Context payloads carry machine cents; prompt text gets human dollars."""
    out = {k: v for k, v in summary.items() if k != "amount_cents"}
    out["amount"] = dollars(summary["amount_cents"])
    return out
```

In `autoreview_items`, the transfer_pair branch becomes:

```python
        elif item.kind == ReviewKind.transfer_pair:
            if not context.get("candidates"):
                continue
            prompt = _TRANSFER_PROMPT.format(
                outflow=_prompt_txn(context["outflow"]) if context.get("outflow") else None,
                candidates=[_prompt_txn(c) for c in context["candidates"]],
            )
```

and the recurring_cluster branch's samples line becomes:

```python
                samples=[_prompt_txn(s) for s in context.get("samples", [])],
```

In `verify_series`, the `_VERIFY_PROMPT.format(...)` samples argument becomes:

```python
                samples=[
                    _prompt_txn(_sample(t))
                    for t in await _recent_occurrences(session, series.id)
                ],
```

In `review_context`'s price_change branch, replace the two `dollars()` lines:

```python
            "old_amount_cents": old if isinstance(old, int) else None,
            "new_amount_cents": new if isinstance(new, int) else None,
```

- [ ] **Step 4: Update CLI rendering in `src/moneta/cli/main.py::_review_one`**

recurring_cluster and price_change sample loops (two places):

```python
        for s in ctx.get("samples", []):
            console.print(f"    {s['posted_on']}  {fmt_money(abs(s['amount_cents']))}")
```

price_change old→new line (display stays unsigned like today; test_cli pins `$15.99 → $18.99`):

```python
        old, new = ctx.get("old_amount_cents"), ctx.get("new_amount_cents")
        console.print(
            f"    {fmt_money(abs(old)) if isinstance(old, int) else '?'} → "
            f"{fmt_money(abs(new)) if isinstance(new, int) else '?'} on {ctx.get('occurred_on')}"
        )
```

transfer_pair outflow and candidate lines:

```python
            console.print(
                f"    out: {fmt_money(abs(outflow['amount_cents']))} on {outflow['posted_on']} "
                f"from {outflow['account']} — {outflow['description']!r}"
            )
```

```python
                console.print(
                    f"    {n}. {fmt_money(abs(c['amount_cents']))} on {c['posted_on']} "
                    f"into {c['account']} — {c['description']!r}"
                )
```

- [ ] **Step 5: Run the three test files**

Run: `uv run pytest tests/test_api.py tests/test_autoreview.py tests/test_cli.py -q`
Expected: PASS (including `test_verify_prompt_carries_amounts_and_dates` untouched and `"$15.99 → $18.99"` in the CLI price-change test)

- [ ] **Step 6: Full gate + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/review.py src/moneta/cli/main.py tests/test_api.py tests/test_autoreview.py
git commit -m "feat(api): review context in cents; prompts keep dollars at the boundary"
```

---

### Task 3: Power report in cents + sign-consistent rendering

**Files:**
- Modify: `src/moneta/views/power.py`
- Modify: `src/moneta/cli/main.py::power` (~lines 86–104)
- Test: `tests/test_power.py`, `tests/test_api.py`, `tests/test_e2e.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `fmt_money` from Task 1.
- Produces: `PowerReport` fields `monthly_income_cents: int`, `income_sources: list[SeriesLine]`, `fixed_costs: list[SeriesLine]`, `total_fixed_cents: int`, `spending_power_cents: int`, `spent_so_far_cents: int`, `remaining_cents: int`; `SeriesLine` fields `merchant: str`, `cadence: Cadence`, `monthly_cents: int` (unsigned magnitude, as today's `abs()`).

- [ ] **Step 1: Update assertions to cents (failing first)** — `tests/test_power.py`:

```python
    assert report.monthly_income_cents == 541667  # 2500 * 26/12, cents-rounded
    assert report.total_fixed_cents == 181599
    assert report.spending_power_cents == 360068
    assert report.spent_so_far_cents == 4500
    assert report.remaining_cents == 355568
```

line ~54: `income = [(line.merchant, line.cadence, line.monthly_cents) for line in report.income_sources]` (the tuple values in that test's expected list change from `Decimal` to int cents — multiply each expected dollar amount by 100).
line ~80: `assert report.total_fixed_cents == 0  # CC payment series filtered out`
line ~92: `assert report.total_fixed_cents == 0`
line ~113: `assert report.total_fixed_cents == 13500`
line ~122: `assert r.spent_so_far_cents == 5000`
Drop the now-unused `from decimal import Decimal` import if nothing else in the file uses it.

`tests/test_api.py` line ~85: `assert r.json()["total_fixed_cents"] == 1599`
lines ~165/173: `assert (await client.get("/power")).json()["total_fixed_cents"] == 1599` / `== 0`

`tests/test_e2e.py` line ~134: `assert power["spent_so_far_cents"] == 6240`

Append to `tests/test_api.py` (uses the existing `client` fixture):

```python
async def test_power_money_fields_are_ints(client: httpx.AsyncClient) -> None:
    body = (await client.get("/power")).json()
    for key in (
        "monthly_income_cents",
        "total_fixed_cents",
        "spending_power_cents",
        "spent_so_far_cents",
        "remaining_cents",
    ):
        assert isinstance(body[key], int), key
```

Append to `tests/test_cli.py` (sign ticket's acceptance test — `_isolate`/`_seed_db`/`make_series` already imported there):

```python
def test_power_negative_money_renders_minus_before_dollar(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_series(session, merchant="Rent", expected_cents=-500000)

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    assert "-$5000.00" in result.output  # fixed costs / spending power / remaining
    assert "$-" not in result.output  # one sign format everywhere
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_power.py tests/test_api.py tests/test_e2e.py tests/test_cli.py -q`
Expected: FAIL — `AttributeError`/`KeyError` on the new `_cents` names.

- [ ] **Step 3: Rewrite `src/moneta/views/power.py` models and totals**

```python
class SeriesLine(BaseModel):
    merchant: str
    cadence: Cadence
    monthly_cents: int


class PowerReport(BaseModel):
    month: str
    monthly_income_cents: int
    income_sources: list[SeriesLine]
    fixed_costs: list[SeriesLine]
    total_fixed_cents: int
    spending_power_cents: int
    spent_so_far_cents: int
    remaining_cents: int


def _series_lines(series: Iterable[RecurringSeries]) -> tuple[list[SeriesLine], int]:
    lines = [
        SeriesLine(merchant=s.merchant, cadence=s.cadence, monthly_cents=abs(monthly_cents(s)))
        for s in series
    ]
    lines.sort(key=lambda line: line.monthly_cents, reverse=True)
    return lines, sum(line.monthly_cents for line in lines)
```

`power_report` tail (the query block above it is unchanged):

```python
    spent_cents = sum(-t.amount_cents for t in month_txns if t.id not in linked_ids)

    power = monthly_income - total_fixed
    return PowerReport(
        month=f"{today.year:04d}-{today.month:02d}",
        monthly_income_cents=monthly_income,
        income_sources=income,
        fixed_costs=fixed,
        total_fixed_cents=total_fixed,
        spending_power_cents=power,
        spent_so_far_cents=spent_cents,
        remaining_cents=power - spent_cents,
    )
```

Remove `from decimal import Decimal` and drop `from_cents` from the `moneta.models` import list.

- [ ] **Step 4: Rewrite the CLI `power` table** in `src/moneta/cli/main.py`:

```python
@app.command()
def power() -> None:
    """Monthly spending power: income - fixed costs."""
    r = request("GET", "/power")
    table = Table(title=f"Spending power — {r['month']}", show_header=False)
    table.add_row("Income (detected)", f"{fmt_money(r['monthly_income_cents'])}/mo")
    for line in r["income_sources"]:
        table.add_row(
            f"  {escape(line['merchant'])} ({line['cadence']})", fmt_money(line["monthly_cents"])
        )
    table.add_row("Fixed costs", f"{fmt_money(-r['total_fixed_cents'])}/mo")
    for line in r["fixed_costs"]:
        table.add_row(
            f"  {escape(line['merchant'])} ({line['cadence']})", fmt_money(line["monthly_cents"])
        )
    table.add_row(
        "[bold]Spending power[/bold]", f"[bold]{fmt_money(r['spending_power_cents'])}/mo[/bold]"
    )
    table.add_row("Spent so far", fmt_money(-r["spent_so_far_cents"]))
    table.add_row("[bold]Remaining[/bold]", f"[bold]{fmt_money(r['remaining_cents'])}[/bold]")
    console.print(table)
```

(The "Fixed costs" and "Spent so far" rows negate positive magnitudes — same visible minus as today, now via `fmt_money`.)

- [ ] **Step 5: Run the four test files**

Run: `uv run pytest tests/test_power.py tests/test_api.py tests/test_e2e.py tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 6: Full gate + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/views/power.py src/moneta/cli/main.py tests/test_power.py tests/test_api.py tests/test_e2e.py tests/test_cli.py
git commit -m "feat(api): power report in integer cents; one sign format in the CLI table"
```

---

### Task 4: Net worth in cents

**Files:**
- Modify: `src/moneta/views/networth.py`
- Modify: `src/moneta/cli/main.py::networth` (~lines 107–127)
- Test: `tests/test_networth.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `fmt_money` from Task 1.
- Produces: `NetWorthReport` fields `liquid_cents: int`, `vested_holdings_cents: int`, `liabilities_cents: int` (positive magnitude, as today), `net_worth_cents: int`, `unvested_potential_cents: int`; `unknown_accounts`/`foreign_accounts` unchanged.

- [ ] **Step 1: Update assertions (failing first)** — `tests/test_networth.py` (dollar → cents ×100):

```python
    assert r.liquid_cents == 500000
    assert r.liabilities_cents == 120000
    assert r.vested_holdings_cents == 400000  # 40/100 of $10,000
    assert r.unvested_potential_cents == 600000
    assert r.net_worth_cents == 780000
```

line ~40: `assert r.vested_holdings_cents == 250000 and r.unvested_potential_cents == 0`
line ~46: `assert r.unknown_accounts == 1 and r.net_worth_cents == 0`
lines ~62–63: `assert r.vested_holdings_cents == 1000000` (clamped) / `assert r.net_worth_cents == 1000000`
line ~70: `assert r.liquid_cents == 100000`
Drop the `Decimal` import if now unused.

Append to `tests/test_api.py`:

```python
async def test_networth_money_fields_are_ints(client: httpx.AsyncClient) -> None:
    body = (await client.get("/networth")).json()
    for key in (
        "liquid_cents",
        "vested_holdings_cents",
        "liabilities_cents",
        "net_worth_cents",
        "unvested_potential_cents",
    ):
        assert isinstance(body[key], int), key
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_networth.py tests/test_api.py -q`
Expected: FAIL on the new names.

- [ ] **Step 3: Rewrite `src/moneta/views/networth.py` model + return**

```python
class NetWorthReport(BaseModel):
    liquid_cents: int
    vested_holdings_cents: int
    liabilities_cents: int
    net_worth_cents: int
    unvested_potential_cents: int
    unknown_accounts: int
    foreign_accounts: int
```

```python
    return NetWorthReport(
        liquid_cents=liquid,
        vested_holdings_cents=vested_cents,
        liabilities_cents=liabilities,
        net_worth_cents=liquid + vested_cents - liabilities,
        unvested_potential_cents=unvested_cents,
        unknown_accounts=unknown,
        foreign_accounts=len(accounts) - len(domestic),
    )
```

Remove the `Decimal` import and `from_cents` from the models import.
Note: `liquid`/`liabilities` come from `sum(...)` over ints — if mypy complains about `int | Literal[0]`, they're already plain ints; no cast needed.

- [ ] **Step 4: Update the CLI `networth` table**

```python
    table = Table(title="Net worth", show_header=False)
    table.add_row("Liquid", fmt_money(r["liquid_cents"]))
    table.add_row("Vested holdings", fmt_money(r["vested_holdings_cents"]))
    table.add_row("Liabilities", fmt_money(-r["liabilities_cents"]))
    table.add_row("[bold]Net worth[/bold]", f"[bold]{fmt_money(r['net_worth_cents'])}[/bold]")
    table.add_row("Unvested (potential)", fmt_money(r["unvested_potential_cents"]))
```

(The two `unknown_accounts`/`foreign_accounts` warnings below the table are untouched.)

- [ ] **Step 5: Run + full gate + commit**

```bash
uv run pytest tests/test_networth.py tests/test_api.py tests/test_cli.py -q
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/views/networth.py src/moneta/cli/main.py tests/test_networth.py tests/test_api.py
git commit -m "feat(api): net worth report in integer cents"
```

---

### Task 5: Cashflow in cents

**Files:**
- Modify: `src/moneta/views/cashflow.py` (both functions return `int`)
- Modify: `src/moneta/api.py::CashflowReport` (~lines 107–111)
- Modify: `src/moneta/cli/main.py::cashflow` (~lines 179–183)
- Test: `tests/test_cashflow.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `fmt_money` from Task 1.
- Produces: `accrual_spend(...) -> int`, `cash_out(...) -> int` (cents, positive magnitudes as today); `CashflowReport` fields `start: date`, `end: date`, `accrual_cents: int`, `cash_out_cents: int`.

- [ ] **Step 1: Update assertions (failing first)** — `tests/test_cashflow.py`:

lines ~37/42: `== 8000` (was `Decimal("80")`); lines ~60–61/92: `== 0`; endpoint test lines ~79–80:

```python
    assert body["accrual_cents"] == 8000  # the RESTAURANT purchase
    assert body["cash_out_cents"] == 8000  # the CC PAYMENT, not the purchase
```

Drop the `Decimal` import if now unused.

`tests/test_cli.py::test_cashflow_date_flags_pass_params` fake response (~line 203) and output assertion (~line 209):

```python
        return {"start": "2026-01-01", "end": "2026-06-30", "accrual_cents": 1234, "cash_out_cents": 500}
```

```python
    assert "$12.34" in result.output
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_cashflow.py tests/test_cli.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`src/moneta/views/cashflow.py`: change both signatures to `-> int`, both tails to `return total`, delete the `Decimal` import and `from_cents` from the models import.

`src/moneta/api.py`:

```python
class CashflowReport(BaseModel):
    start: date
    end: date
    accrual_cents: int
    cash_out_cents: int
```

and in the `/cashflow` endpoint construction: `accrual_cents=await accrual_spend(...)`, `cash_out_cents=await cash_out(...)` (call arguments unchanged). Delete `from decimal import Decimal` from api.py if now unused.

`src/moneta/cli/main.py::cashflow`:

```python
    table.add_row("Accrual spend", fmt_money(r["accrual_cents"]))
    table.add_row("Cash out", fmt_money(r["cash_out_cents"]))
```

- [ ] **Step 4: Run + full gate + commit**

```bash
uv run pytest tests/test_cashflow.py tests/test_cli.py -q
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/views/cashflow.py src/moneta/api.py src/moneta/cli/main.py tests/test_cashflow.py tests/test_cli.py
git commit -m "feat(api): cashflow in integer cents"
```

---

### Task 6: Obligations in cents

**Files:**
- Modify: `src/moneta/views/financing.py`
- Modify: `src/moneta/cli/main.py::obligations` (~lines 186–204)
- Test: `tests/test_financing.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `fmt_money` from Task 1.
- Produces: `Obligation` fields `balance_owed_cents: int` (positive magnitude), `monthly_payment_cents: int | None` (positive magnitude); `months_left`, `payoff_estimate`, `promo_expires_on`, `deferred_interest_risk` unchanged.

- [ ] **Step 1: Update assertions (failing first)** — `tests/test_financing.py`:

```python
    assert ob.balance_owed_cents == 121500
    assert ob.monthly_payment_cents == 13500
```

line ~68: `assert obs[0].monthly_payment_cents is None and obs[0].months_left is None`
Drop the `Decimal` import if now unused.

Append the endpoint-level int pin to `tests/test_api.py` (a bare loan account is enough — no payment series needed):

```python
async def test_obligations_money_fields_are_ints(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from moneta.models import AccountType

    async with sessionmaker() as session:
        await make_account(session, type=AccountType.loan, balance_cents=-121500)
        await session.commit()

    body = (await client.get("/obligations")).json()
    assert body[0]["balance_owed_cents"] == 121500  # int, not "1215.00"
    assert body[0]["monthly_payment_cents"] is None
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_financing.py tests/test_api.py -q`
Expected: FAIL.

- [ ] **Step 3: Rewrite `src/moneta/views/financing.py` money handling**

```python
class Obligation(BaseModel):
    account_id: int
    account_name: str
    balance_owed_cents: int
    monthly_payment_cents: int | None
    months_left: int | None
    payoff_estimate: date | None
    promo_expires_on: date | None
    deferred_interest_risk: bool
```

In `compute_obligations`:

```python
        balance_owed_cents = abs(loan.balance_cents)
        payment_cents: int | None = None
        months_left: int | None = None
        payoff: date | None = None
        series_id = _payment_series_id(loan.id, links)
        if series_id is not None:
            series = (
                await session.execute(
                    select(RecurringSeries).where(RecurringSeries.id == series_id)
                )
            ).scalar_one()
            payment_cents = abs(monthly_cents(series))
            if payment_cents > 0:
                months_left = math.ceil(balance_owed_cents / payment_cents)
                payoff = today + timedelta(days=30 * months_left)
```

and the constructor: `balance_owed_cents=balance_owed_cents, monthly_payment_cents=payment_cents` (other fields unchanged). `months_left` is identical to before — the cents/cents ratio equals the old dollars/dollars ratio. Remove the `Decimal` import and `from_cents` from the models import.

- [ ] **Step 4: Update the CLI `obligations` table**

```python
        table.add_row(
            escape(ob["account_name"]),
            fmt_money(ob["balance_owed_cents"]),
            fmt_money(ob["monthly_payment_cents"]) if ob["monthly_payment_cents"] else "?",
            str(ob["months_left"] or "?"),
            f"{payoff}{warn}",
            str(ob["promo_expires_on"] or "—"),
        )
```

- [ ] **Step 5: Run + full gate + commit**

```bash
uv run pytest tests/test_financing.py tests/test_cli.py -q
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/views/financing.py src/moneta/cli/main.py tests/test_financing.py tests/test_api.py
git commit -m "feat(api): obligations in integer cents"
```

---

### Task 7: Accounts balance + series expected in cents

**Files:**
- Modify: `src/moneta/api.py` (`AccountOut` ~line 46, `SeriesOut` ~line 60, `/accounts` ~line 294, `/recurring` ~line 240)
- Modify: `src/moneta/cli/main.py` (`recurring` table ~line 159, `accounts` table ~line 226)
- Test: `tests/test_api.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `fmt_money` from Task 1.
- Produces: `AccountOut.balance_cents: int` (signed, straight from `Account.balance_cents`); `SeriesOut` loses `expected_amount` (`expected_cents: int` remains, signed).

- [ ] **Step 1: Write the pinning tests (failing first)** — append to `tests/test_api.py`:

```python
async def test_accounts_and_recurring_money_fields_are_ints(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        await make_account(session, balance_cents=-121500)
        await make_series(session, expected_cents=-1599)
        await session.commit()

    accounts = (await client.get("/accounts")).json()
    assert accounts[0]["balance_cents"] == -121500
    assert "balance" not in accounts[0]

    series = (await client.get("/recurring")).json()
    assert series[0]["expected_cents"] == -1599
    assert "expected_amount" not in series[0]
```

(`make_account`/`make_series` are already imported in test_api.py; add them to the import list if not.)

Then grep both test files for uses of the removed fields and update them:

```bash
grep -n "expected_amount\|\"balance\"\|\['balance'\]" tests/
```

Any CLI test asserting a rendered balance (e.g. the obligations/accounts flows around `tests/test_cli.py:579`) keeps passing if it asserted formatted output like `$1215.00` — the rendering below preserves it; update only assertions on raw payload keys.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_api.py tests/test_cli.py -q`
Expected: new test FAILS (`balance_cents` missing).

- [ ] **Step 3: Implement in `src/moneta/api.py`**

```python
class AccountOut(BaseModel):
    id: int
    name: str
    org_name: str
    type: AccountType
    balance_cents: int
    promo_expires_on: date | None
```

```python
class SeriesOut(BaseModel):
    id: int
    merchant: str
    direction: str
    cadence: str
    expected_cents: int
    next_expected_on: date
    status: str
```

`/accounts` constructor: `balance_cents=a.balance_cents` (drop the f-string). `/recurring` constructor: delete the `expected_amount=...` line.

- [ ] **Step 4: Update the CLI tables**

`recurring` Expected column (display stays unsigned; the Direction column carries the sign):

```python
                fmt_money(abs(s["expected_cents"])),
```

`accounts` Balance column (signed — a negative credit-card balance should show the minus):

```python
            fmt_money(a["balance_cents"]),
```

- [ ] **Step 5: Run + full gate + commit**

```bash
uv run pytest tests/test_api.py tests/test_cli.py -q
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/api.py src/moneta/cli/main.py tests/test_api.py tests/test_cli.py
git commit -m "feat(api): accounts balance and series expected as raw cents; drop display strings"
```

---

### Task 8: Dead-code sweep, docs, ticket cleanup

**Files:**
- Modify: `src/moneta/models.py` (delete `from_cents` if orphaned)
- Modify: `docs/PRD.md`, `docs/user-guide.md`, `README.md` (as grep dictates)
- Delete: `docs/backlog/medium/normalize-api-money-representation.md`, `docs/backlog/low/power-sign-rendering-inconsistency.md`

- [ ] **Step 1: Sweep `from_cents`**

```bash
grep -rn "from_cents" src/ tests/
```

Expected after Tasks 3–6: only the definition in `src/moneta/models.py` (and possibly test imports to drop). Delete the `from_cents` function; keep `to_cents` (input boundary) and `dollars` (prompt-only — update its docstring to say so):

```python
def dollars(cents: int) -> str:
    """Unsigned display amount for LLM prompt text only — API payloads carry cents."""
    return f"{abs(cents) / 100:.2f}"
```

- [ ] **Step 2: Docs**

```bash
grep -n "expected_amount\|total_fixed\|spent_so_far\|accrual\|balance\b\|\$-" docs/user-guide.md docs/PRD.md README.md
```

- `docs/PRD.md`: line ~143 lists "normalize API money representation" as backlog — remove it there; add a feature-history row: "API money convention: every response money field is integer cents (`*_cents`); CLI owns formatting."
- `docs/user-guide.md`: update any API/JSON examples showing money as strings to cents ints; note the convention in the API/config reference section.
- `README.md`: only if a sample output shows the old `$-` rendering.

- [ ] **Step 3: Delete the shipped tickets**

```bash
git rm docs/backlog/medium/normalize-api-money-representation.md docs/backlog/low/power-sign-rendering-inconsistency.md
```

- [ ] **Step 4: Full gate + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add -A
git commit -m "chore: drop from_cents, update docs, close wave-1 backlog tickets"
```

---

### Task 9: Review gates + PR

Per the global superpowers conventions — these are skill/agent invocations, not code steps:

- [ ] **Step 1:** `@feature-dev:code-architect` review of the branch diff; fix every issue it raises.
- [ ] **Step 2:** `/simplify` pass over the diff; apply its findings.
- [ ] **Step 3:** superpowers:requesting-code-review / verification-before-completion — run the full gate one final time and confirm output before claiming done.
- [ ] **Step 4:** QA-backlog subagent: review the diff for anything needing manual verification (expected: none — pure representation change covered by tests; if so, state that explicitly instead of filing items).
- [ ] **Step 5:** claude-md check: CLAUDE.md's conventions section — add one line to "Conventions that aren't obvious from the code": API money = integer cents `*_cents`; CLI formats via `fmt_money`.
- [ ] **Step 6:** Push and open the PR:

```bash
git push -u origin feature/api-money-cents
gh pr create --title "API money normalization: integer cents everywhere" --body "$(cat <<'EOF'
## Summary
- Every API money field is now integer cents named `*_cents` (was a mix of Decimal-as-JSON-string, pre-formatted display strings, and raw cents)
- One `fmt_money` helper in the CLI; negatives render `-$X.YY` everywhere (fixes the power-table sign inconsistency)
- Review-context payloads carry cents; LLM prompt text keeps human dollars at the prompt boundary

Closes backlog: normalize-api-money-representation, power-sign-rendering-inconsistency.
Spec: docs/superpowers/specs/2026-07-15-api-money-normalization-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
