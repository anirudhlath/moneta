# Loan and Financing Obligations Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Loan and financing obligations report the payment actually being made,
warn about promo ends in words that are true for every promo, let a promo date be
cleared, list bills due today as upcoming, count a card with an unanswered
financing question as financing, and stop counting a financing card's purchases as
spend on top of the payments that pay them off.

**Architecture:**
- `queries.loan_payment_stats` keeps its signature. Payments are summed per day, and
  the amount comes from a new private `_current_payment` rule (newest payment, unless
  it is a one-off extra).
- The warning predicate and the API field are unchanged. Only the CLI and digest
  text change, and a comment documents what the field means.
- `LogicalAccount` gains `financing_pending` and a `loan_like` property. These are
  computed once in `load_logical_data` from the review ledger, and they replace the
  two hand-written `type == loan or financing_mode` predicates. Every consumer of
  `inflow_is_loan_like` then treats an open question the way it treats
  `financing_mode`.
- `spend_reason` gains the account's `loan_like` and returns a new reason,
  `financed purchase`, for that account's own outflows. `power`'s spent-so-far and
  the `txns` trust view both read it. `views/cashflow.py` has its own copy of the
  spend rule, used by `power --history` and `cashflow`, and its two accrual filters
  drop the same outflows.
- The CLI's `--set-promo` maps `none` or an empty value to `null`. The `upcoming`
  window becomes `[today, month_end]`.

**Tech Stack:** Python 3.13, FastAPI, typer, SQLAlchemy async + SQLite, pytest
(asyncio auto mode), uv.

**Spec:** GitHub issues #44 (store-card financing end to end) and #32 (an old
merged payment series heals itself), plus the **Background** section of this plan.
There is no design doc. Background holds each defect's root cause and the owner's
decision.

## Global Constraints

- Money is integer cents. Negative means outflow. `LoanPayment.expected_cents` is
  negative. `Obligation.monthly_payment_cents` and `UpcomingCharge.expected_cents`
  are unsigned magnitudes.
- **Payment amount rule:** the newest in-run payment, unless it is **strictly more
  than 1.5×** the median of **up to 3** in-run payments just before it. In that case
  the obligation is that median, rounded with `round()`. Payments to one account on
  one calendar day count as **one payment, their sum**, for both cadence and amount.
- **Warning text, verbatim:** `! payoff lands after the promo ends — interest applies to the remaining balance (back-dated on deferred-interest plans)`.
- The API field keeps the name `deferred_interest_risk`, and no JSON shape changes.
- `--set-promo ID VALUE` clears the date when `VALUE.strip().lower()` is `""` or
  `"none"`. Any other value must parse as `YYYY-MM-DD`, or the command fails before
  any request.
- The upcoming window is `today <= due <= month_end`, for series and derived loan
  payments alike.
- An account counts as loan-like when it has an open `financing_account` question
  and no financing ruling exists on any member of its logical account. A loan-like
  account behaves exactly as `financing_mode = True` does.
- A loan-like account's own outflows (purchases, interest, fees) are never spend,
  because its payments are the fixed cost. `spend_reason` returns
  `financed purchase` for them, checked after `non-spend account` and before
  `foreign currency`, so a `loan`-type account keeps `non-spend account`.
  `cashflow`'s accrual filters drop them too. Inflows on such an account are
  unchanged.
- Views don't commit; pipelines do. Enum columns load as plain `str`, so compare
  them with `==`.
- The repo is **public**. Use invented names only (Acme, Corner Curry, Corner Gym)
  and round amounts, in code, tests, docs and commit messages.
- Gate before every commit (all four, with pristine output; a warning counts as a failure):
  `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  During red/green steps, run only the narrow test file.
- Commit with `git commit -s`, with no AI co-author trailers. Work on branch
  `fix-obligations`.

## Review Focus

1. **Payments on the same day**, such as a payment split in two or a regular
   payment plus an extra on one day. Today these are two list entries against one
   cadence date. They must become one payment of their sum. Pinned in Task 1
   (`test_loan_payment_stats_sums_payments_on_one_day`).
2. **A loan with exactly one payment** must still get an obligation. That payment
   is the amount, on the monthly fallback cadence, with no index error from the
   empty prior window. Pinned in Task 1
   (`test_loan_payment_stats_expected_is_newest_unless_one_off[amounts2--5000]`).
3. **A bill due on `month_end`, due today, or charged today.** The first two are
   listed. A bill charged today is not listed again, because detection has already
   moved its next date forward. Pinned in Task 4
   (`test_upcoming_includes_series_inside_window_excludes_outside`,
   `test_bill_charged_today_is_not_also_upcoming`,
   `test_upcoming_loan_payment_due_today_but_not_one_paid_today`).
4. **A promo date equal to today** while a balance remains. The warning shows,
   because payoff is always later than today. Pinned in Task 2
   (`test_promo_ending_today_with_a_balance_left_warns`).
5. **A financing question resolved, then reopened**, which in practice means a
   second open question on another provider's record of a card a human already
   ruled on. The ruling stands, and the card stays a plain card. Pinned in Task 5
   (`test_financing_ruling_beats_an_open_question_on_an_alias`).
6. **A known gap: a new financed purchase before the card's first payment.** It is
   not spend, and the payment line that stands in for it only appears once a linked
   payment exists. Until then the purchase shows in no `power` figure, although
   `moneta obligations` lists the card's balance with no payment yet. This plan
   accepts that and documents it in Task 6, in the user guide's reason table and
   the PRD. The first linked payment closes the gap, and a single payment is
   enough (Review Focus 2). Pinned in Task 6, where
   `test_financed_purchase_is_not_also_spend` uses exactly one payment.

## Background

Every claim below was checked at commit `eed9005`, and line numbers are from that
commit.

**F1. The payment amount lags behind the payments.**
`queries.loan_payment_stats` (`src/moneta/queries.py:165-192`) sets
`expected_cents=-round(statistics.median(amounts))` (line 189) over every in-run
payment. Two places consume it: `views/financing.py:41-45` turns it into
`monthly_payment_cents`, months left and the payoff date, and
`views/power.py:125-150` turns it into the derived fixed-cost line and its upcoming
date. When payments fall, as on a plan nearing payoff, the median overstates the
obligation. That pulls the payoff estimate earlier, which can hide a promo warning.
The same code also keeps `dates` as a set (line 177) but `amounts` as a list (lines
183-185). Two payments on one day therefore count as one cadence date but as two
amounts.
*Owner's decision:* the obligation is the newest in-run payment, unless it is more
than 1.5× the median of up to 3 prior in-run payments; then it is that median (a
one-off extra shouldn't set the obligation).
*Decision made in this plan:* same-day payments are summed into one payment before
cadence and amount are judged. Exactly 1.5× keeps the newest payment ("more than").

**F6. The promo warning wording is wrong.** The predicate (`views/financing.py:46-50`,
payoff later than `promo_expires_on`) is right. The text, however, says "deferred
interest" (`cli/main.py:542`, `pipelines/digest.py:63`). That is false for a true
0% promo, which charges interest only from its end date.
*Owner's decision:* use the neutral text in Global Constraints. Keep the API field
name `deferred_interest_risk` (`views/financing.py:21`) for compatibility, and
document what it means. The docs that carry the old wording are README 57 and 205,
user guide 290, 317, 458 and 512, and PRD 95 and 122.

**F5. A promo date can't be cleared from the CLI.** `accounts --set-promo ID DATE`
(`cli/main.py:592`) always calls `_parse_iso_date` (`cli/main.py:31-36`), so
`none` fails as an invalid date. The API already accepts null.
`PATCH /accounts/{id}` with `{"promo_expires_on": null}` clears the column and
records a resolved "promotion metadata" decision with a null value
(`api.py:598-611`). `account_metadata.effective_account_metadata` honors that
decision across re-syncs.
*Owner's decision:* `None if value.strip().lower() in {"", "none"} else _parse_iso_date(value)`.
Update the help text (`cli/main.py:567`) and the user guide.

**F7. The upcoming list skips today.** A strict `today <` test drops a bill due
today and not yet charged. It sits at `views/power.py:114` for series and
`views/power.py:143` for loans. Three places pin it: `tests/test_power.py:328-337`,
spec `docs/superpowers/specs/2026-07-16-visibility-power-polish-design.md` §5
(line 106), and PRD line 153.
Nothing is counted twice. Detection sets `next_expected_on` to
`max(old, advance_expected_on(newest charge))` (`pipelines/recurring.py:316, 381,
415`), so a bill charged today moves a period forward. A loan paid today projects
`advance_expected_on(today)`, which is later than today. `upcoming` is a list and is
never summed into `remaining`.
*Owner's decision:* `today <= due <= month_end`.

**F2. Unanswered financing accounts don't count as financing.** Auto-review skips
`financing_account` questions (`src/moneta/pipelines/review.py:577`, the
`else: continue` branch). Until a human rules, such a card is plain `credit`. Its
payments are classified as credit-card payments rather than loan payments
(`queries.py:144-145`, `:173`). `power` therefore filters out its payment series as
a card payment (`views/power.py:89-95`) and derives no payment line for it.
`obligations` uses a second, hand-copied predicate (`views/financing.py:30`), so it
omits the card too.
The flag `inflow_is_loan_like` has three further consumers: the trust view's link
label (`views/transactions.py:45`), the derived-line double-count guard
(`views/power.py:123`), and detection's untagging of old payment series
(`pipelines/recurring.py:151`).
*Owner's decision:* while the question is open, treat the account as loan-like.
Mirror a confirmed `financing_mode` account exactly, including how its purchases
are treated, with no double counting. Purchases are the next defect: Task 6 fixes
them for confirmed cards and open-question cards alike, through `loan_like`.
*Decisions made in this plan:*
- A financing ruling recorded on any member of the logical account beats an open
  question on another member.
- The `accounts` table gains no "pending" marker.
- Mirroring also means that the sync which opens the question already retires an
  old payment series over the card. This changes #32's step 6, which says the
  heal waits for the answer.

**F2, continued: a financing card's purchases count twice.** A financing card's
payments count as its fixed cost, yet its own purchases also count as spend.
`spend_reason` (`src/moneta/views/transactions.py:55-84`) asks only whether
`account_type in SPEND_ACCOUNT_TYPES` (line 80), and `credit` is one of those
types, so a $1,000.00 financed purchase is spent in its month and again as the
monthly payment line. The history view has its own copy of the rule:
`views/cashflow.py` filters on account type in `_accrual` (line 36, behind
`cashflow`'s accrual spend) and in `accrual_by_month` (line 99, behind
`power --history`), and neither filter knows about financing.
The user guide already describes the intended design: its payments count "instead
of its purchases" (lines 479 and 513). The code is the bug, and the guide stays.
*Owner's decision:* a loan-like account (`financing_mode`, or per F2 an open
financing question) excludes its own outflows from spend. Those outflows are the
purchases plus interest and fees, and the payments cover them. `spend_reason` takes
a loan-like input and gives the new reason "financed purchase", which reaches power's
`spent_so_far` and the `txns` trust view. `cashflow.py`'s accrual filters apply the
same rule.
*Known gap, accepted:* a new financed purchase shows nowhere in `power` until the
card's first payment (Review Focus 6).
*Decisions made in this plan:*
- The reason is checked after `non-spend account`, so a `loan`-type account's
  outflows keep that reason, and before `foreign currency`.
- `cashflow`'s accrual spend (`_accrual`) gets the rule as well as
  `accrual_by_month`, so `cashflow` and `power --history` agree with `power`.
- Inflows on a loan-like account, such as a refund, are left as they are.
*Conflict:* branch `fix-transfer-scoring` plans to rewrite `accrual_by_month` in
its Task 4, replacing the imports, the docstring and the bucketing loop and changing
the return type to a four-field `MonthFlows`. Task 6 here touches only the
function's `txns` filter, plus one `financed` set built just above it. The comment
goes on the filter, not in the docstring, so that rewrite drops nothing. Whichever
branch merges second must:
- keep the `financed` filter line;
- change the new `test_accrual_excludes_financed_purchase` expectation
  `("2026-07", 0, spend)` to the four-field shape.

*Note for the QA issues:* #44's step 4 quotes the old summary line. After Task 2
merges, its quote should read the new text.

## File map

| File | Change | Task |
|---|---|---|
| `src/moneta/queries.py` | `_current_payment` rule, same-day sums in `loan_payment_stats`; `inflow_is_loan_like = in_account.loan_like` | 1, 5 |
| `src/moneta/views/financing.py` | field comment on `deferred_interest_risk`; `a.loan_like` filter | 2, 5 |
| `src/moneta/cli/main.py` | warning text; `--set-promo none` | 2, 3 |
| `src/moneta/pipelines/digest.py` | digest warning text | 2 |
| `src/moneta/views/power.py` | `<=` window bounds | 4 |
| `src/moneta/account_metadata.py` | `open_financing_roots` | 5 |
| `src/moneta/logical.py` | `LogicalAccount.financing_pending`, `.loan_like` | 5 |
| `src/moneta/views/transactions.py` | `spend_reason(account_loan_like=...)`, `financed purchase` | 6 |
| `src/moneta/views/power.py` (spent-so-far call) | pass `account.loan_like` | 6 |
| `src/moneta/views/cashflow.py` | `financed` filter in `_accrual` and `accrual_by_month` | 6 |
| `tests/test_queries.py`, `tests/test_financing.py`, `tests/test_power.py`, `tests/test_cli.py`, `tests/test_digest.py`, `tests/test_api.py`, `tests/test_recurring.py`, `tests/test_transactions.py`, `tests/test_cashflow.py` | tests | 1-6 |
| `CLAUDE.md`, `README.md`, `docs/PRD.md`, `docs/user-guide.md`, the 2026-07-16 visibility spec | docs upkeep | 1-6 |

---

### Task 1: A loan's payment is its newest payment (F1)

**Files:**
- Modify: `src/moneta/queries.py:165-192`
- Test: `tests/test_queries.py`, `tests/test_financing.py`
- Docs: `CLAUDE.md:43`, `docs/user-guide.md:466`, `docs/PRD.md:95` and `:130`

**Interfaces:**
- Produces: `loan_payment_stats(links: list[ClassifiedLink]) -> dict[int, LoanPayment]`.
  The signature is unchanged. `expected_cents` follows the rule in Global
  Constraints, and `last_paid_on` is still the newest payment date. The private
  `_current_payment(amounts: list[int]) -> int` takes magnitudes, oldest first,
  and returns a magnitude.

- [ ] **Step 1: Write the failing tests.** In `tests/test_queries.py`, replace:

```python
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession
```

  With:

```python
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
```

  Then change the assertion in
  `test_loan_payment_stats_two_payments_falls_back_monthly`. Replace:

```python
    assert stats[20].expected_cents == -20000  # median(19900, 20100)
```

With:

```python
    assert stats[20].expected_cents == -20100  # the newest payment, within 1.5x of the prior
```

  Append to `tests/test_queries.py`:

```python
def _monthly_links(account_id: int, amounts: list[int]) -> list[ClassifiedLink]:
    """One loan-like payment on the 1st of each month from 2026-04, oldest first."""
    return [
        _link(
            outflow_id=100 + i,
            inflow_id=200 + i,
            inflow_account_id=account_id,
            outflow_amount_cents=amount,
            outflow_posted_on=date(2026, 4 + i, 1),
            inflow_is_loan_like=True,
        )
        for i, amount in enumerate(amounts)
    ]


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        ([-12000, -11000, -10000, -7000], -7000),  # winding down: the newest is the obligation
        ([-5000, -5000, -5000, -20000], -5000),  # a one-off extra doesn't set it
        ([-5000], -5000),  # one payment is its own obligation
        ([-5000, -9000], -5000),  # 9000 > 1.5 x 5000: an extra
        ([-10000, -10000, -10000, -15000], -15000),  # exactly 1.5x is not "more than"
        ([-10000, -10000, -10000, -15001], -10000),
        # only the 3 payments before the newest are the yardstick, not older history
        ([-20000, -20000, -20000, -8000, -8000, -8000, -13000], -8000),
    ],
)
def test_loan_payment_stats_expected_is_newest_unless_one_off(
    amounts: list[int], expected: int
) -> None:
    stats = loan_payment_stats(_monthly_links(40, amounts))

    assert stats[40].expected_cents == expected
    assert stats[40].cadence == Cadence.monthly


def test_loan_payment_stats_sums_payments_on_one_day() -> None:
    """A payment split in two on one day is one payment of the sum, not two halves."""
    links = _monthly_links(50, [-10000, -10000, -5000]) + [
        _link(
            outflow_id=300,
            inflow_id=301,
            inflow_account_id=50,
            outflow_amount_cents=-5000,
            outflow_posted_on=date(2026, 6, 1),
            inflow_is_loan_like=True,
        )
    ]

    stats = loan_payment_stats(links)

    assert stats[50].expected_cents == -10000
    assert stats[50].cadence == Cadence.monthly
    assert stats[50].last_paid_on == date(2026, 6, 1)
```

  Append to `tests/test_financing.py`:

```python
async def test_falling_payments_set_the_obligation_and_its_promo_warning(
    session: AsyncSession,
) -> None:
    """A plan winding down is paid at its newest amount: the payoff estimate (and the
    promo warning) must follow it, not an older median that hides the warning."""
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(
        session,
        type=AccountType.loan,
        name="Acme Furniture Plan",
        balance_cents=-70000,
        promo_expires_on=date(2027, 3, 1),
    )
    for month, amount in ((4, 12000), (5, 11000), (6, 10000), (7, 7000)):
        out = await make_txn(
            session,
            checking,
            amount_cents=-amount,
            merchant="Acme Bank Payment",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(
            session,
            loan,
            amount_cents=amount,
            merchant="Acme Bank Payment",
            posted_on=date(2026, month, 5),
        )
        await _link(session, out, inn)
    await session.flush()

    obs = await compute_obligations(session, today=date(2026, 7, 7))

    assert obs[0].monthly_payment_cents == 7000  # the median, 10500, would say 7 months
    assert obs[0].months_left == 10
    assert obs[0].payoff_estimate == date(2027, 5, 3)  # today + 10 * 30 days
    assert obs[0].deferred_interest_risk is True  # payoff lands after the 2027-03-01 promo end
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_queries.py tests/test_financing.py`.
  Expected: 7 FAIL.
  - `two_payments_falls_back_monthly` (`-20000 == -20100`).
  - Parametrize cases `amounts0` (`-10500 == -7000`), `amounts3` (`-7000 == -5000`),
    `amounts4` (`-10000 == -15000`) and `amounts6` (`-13000 == -8000`).
  - `sums_payments_on_one_day` (`-7500 == -10000`).
  - `falling_payments…` (`10500 == 7000`).

  The other parametrize cases pass already; they pin the rule.

- [ ] **Step 3: Implement.** In `src/moneta/queries.py`, replace the whole
  `loan_payment_stats` function (lines 165-192) with:

```python
# a payment above this multiple of the payments before it is a one-off extra, not the
# new obligation
_ONE_OFF_FACTOR = 1.5
_PRIOR_PAYMENTS = 3  # the yardstick: up to this many payments before the newest


def _current_payment(amounts: list[int]) -> int:
    """What the account is paid now, from its in-run payment magnitudes (oldest first).

    The newest payment, so a plan winding down is not held at its old median — unless it
    is more than _ONE_OFF_FACTOR x the median of the payments just before it, in which
    case that median: a one-off extra must not set the obligation.
    """
    newest = amounts[-1]
    prior = amounts[-1 - _PRIOR_PAYMENTS : -1]
    if prior:
        typical = statistics.median(prior)
        if newest > _ONE_OFF_FACTOR * typical:
            return round(typical)
    return newest


def loan_payment_stats(links: list[ClassifiedLink]) -> dict[int, LoanPayment]:
    """Per-loan-account payment cadence/amount derived from transfer-linked outflows.

    Banks collapse different loans' payments into one descriptor; the link's inflow
    account is the reliable per-loan identity (design 2026-07-16 §3). Payments on one
    day are one payment (a payment split in two), so cadence and amount read the same
    per-day totals.
    """
    by_account: dict[int, dict[date, int]] = {}
    for link in links:
        if link.inflow_is_loan_like:
            days = by_account.setdefault(link.inflow_account_id, {})
            day = link.outflow_posted_on
            days[day] = days.get(day, 0) + abs(link.outflow_amount_cents)
    result: dict[int, LoanPayment] = {}
    for account_id, days in by_account.items():
        dates = sorted(days)
        match = match_cadence(dates)
        if match is None:
            cadence, run_start = Cadence.monthly, dates[0]  # loans are near-universally monthly
        else:
            cadence, run_start = match
        result[account_id] = LoanPayment(
            account_id=account_id,
            cadence=cadence,
            expected_cents=-_current_payment([days[d] for d in dates if d >= run_start]),
            last_paid_on=dates[-1],
        )
    return result
```

- [ ] **Step 4: Run them and confirm they pass.** Run the Step 2 command.
  Expected: PASS (18 tests).

- [ ] **Step 5: Update the docs.**
  - `CLAUDE.md` line 43 (Transfer-link semantics). Replace
    `(cadence + amount stats grouped by inflow account, so a bank collapsing several cards' payments into one shared descriptor can't blend or drop an obligation)`
    with
    `(cadence + amount grouped by inflow account, so a bank collapsing several cards' payments into one shared descriptor can't blend or drop an obligation; same-day payments sum into one, and the amount is the newest payment unless it is more than 1.5× the median of up to 3 payments before it, when that median stands — a one-off extra never sets the obligation)`.
  - `docs/user-guide.md`, after line 466 (`by the linked account keeps each obligation's amount and cadence correct.`).
    Insert a blank line and then:

```markdown
The payment is the account's **newest** payment, so a plan whose payments are winding down is
judged by what you pay now rather than an older average. One exception: a payment more than
1.5× the median of the (up to) three payments before it is a one-off extra — the obligation
stays at that median. Payments to one account on the same day count as one payment, their sum.
```

  - `docs/PRD.md` line 95. Replace `gets payment, balance, months-left` with
    `gets payment (its newest payment, unless that is more than 1.5× the median of up to three before it — a one-off extra; same-day payments sum), balance, months-left`.
  - `docs/PRD.md`. Insert this row directly below the `|---|---|---|` line under
    `| Date | Release | What shipped |`:

```markdown
| 2026-10-15 | **Loan payments follow the newest payment** | A loan or financing account's payment is its newest transfer-linked payment, so a plan winding down shows what is paid now and its promo warning is no longer hidden by an older median. A payment more than 1.5× the median of the (up to) three before it is a one-off extra and leaves the obligation at that median. Payments to one account on one day count as one payment. |
```

- [ ] **Step 6: Run the gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/queries.py tests/test_queries.py tests/test_financing.py CLAUDE.md docs/user-guide.md docs/PRD.md
git commit -s -m "fix: a loan's payment is its newest payment, not the median of its history"
```

---

### Task 2: A neutral promo warning (F6)

**Files:**
- Modify: `src/moneta/cli/main.py:541-542`, `src/moneta/pipelines/digest.py:60-65`,
  `src/moneta/views/financing.py:21`
- Test: `tests/test_cli.py:1524-1568`, `tests/test_digest.py:135, 199, 216`,
  `tests/test_financing.py`
- Docs: `README.md:56-58, 205`, `docs/user-guide.md:290-291, 317, 457-459, 512`,
  `docs/PRD.md:95, 122` and `:130`

**Interfaces:**
- Produces nothing new. `Obligation.deferred_interest_risk: bool` keeps its name
  and its predicate.

- [ ] **Step 1: Write the failing tests.**
  `tests/test_cli.py`: rename the test.

Replace:

```python
def test_obligations_renders_deferred_interest_warning(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
```

With:

```python
def test_obligations_renders_promo_warning(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
```

  Then, in the same test, replace:

```python
    assert "deferred interest" in result.output  # payoff ~9mo out > promo ~2mo out
```

  With:

```python
    assert "promo ends" in result.output  # payoff ~9mo out > promo ~2mo out
    assert "deferred interest risk" not in result.output  # a 0% plan may not be deferred
```

  `tests/test_digest.py`, in `test_warned_set_adds_new_risk_and_suppresses_repeat`.
  Replace:

```python
    assert "Deferred-interest risk" in seen[0].content.decode()
```

  With:

```python
    assert "Promo ends before payoff" in seen[0].content.decode()
    assert "Deferred-interest risk" not in seen[0].content.decode()
```

  In `test_first_run_still_warns_despite_history_baseline`, replace the docstring
  opening `"""Deferred-interest warnings are current-state, not history — the` with
  `"""Promo warnings are current-state, not history — the`. Then replace:

```python
    assert "Deferred-interest risk" in body
```

  With:

```python
    assert "Promo ends before payoff" in body
```

  Append to `tests/test_financing.py` (Review Focus 4):

```python
async def test_promo_ending_today_with_a_balance_left_warns(session: AsyncSession) -> None:
    """A promo that ends today while a balance remains is past saving: the warning holds."""
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(
        session,
        type=AccountType.loan,
        name="Acme Furniture Plan",
        balance_cents=-30000,
        promo_expires_on=date(2026, 7, 7),
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-10000,
            merchant="Acme Bank Payment",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(
            session,
            loan,
            amount_cents=10000,
            merchant="Acme Bank Payment",
            posted_on=date(2026, month, 5),
        )
        await _link(session, out, inn)
    await session.flush()

    obs = await compute_obligations(session, today=date(2026, 7, 7))

    assert obs[0].payoff_estimate == date(2026, 10, 5)  # today + 3 * 30 days
    assert obs[0].deferred_interest_risk is True
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_cli.py tests/test_digest.py tests/test_financing.py -k "promo or warn or risk"`.
  Expected: 3 FAIL: `test_obligations_renders_promo_warning`,
  `test_warned_set_adds_new_risk_and_suppresses_repeat` and
  `test_first_run_still_warns_despite_history_baseline`. The promo-today test passes
  already; it pins the boundary.

- [ ] **Step 3: Implement.** In `src/moneta/cli/main.py`, replace:

```python
    if any(ob["deferred_interest_risk"] for ob in rows):
        console.print("[red]! payoff lands after the promo expires — deferred interest risk[/red]")
```

  With:

```python
    if any(ob["deferred_interest_risk"] for ob in rows):
        console.print(
            "[red]! payoff lands after the promo ends — interest applies to the remaining "
            "balance (back-dated on deferred-interest plans)[/red]"
        )
```

  In `src/moneta/pipelines/digest.py` (`_obligation_line`), replace:

```python
    return (
        f"Deferred-interest risk: {ob.account_name!r} payoff est. {payoff} "
        f"is after promo ends {promo}"
    )
```

  With:

```python
    return (
        f"Promo ends before payoff: {ob.account_name!r} payoff est. {payoff} is after "
        f"promo ends {promo} — interest applies to the remaining balance (back-dated on "
        "deferred-interest plans)"
    )
```

  In `src/moneta/views/financing.py`, replace:

```python
    promo_expires_on: date | None
    deferred_interest_risk: bool
```

  With:

```python
    promo_expires_on: date | None
    # True when payoff_estimate lands after promo_expires_on: interest will apply to the
    # balance left at promo end (back-dated only on deferred-interest plans). The name
    # predates that wording and is kept for API compatibility.
    deferred_interest_risk: bool
```

- [ ] **Step 4: Run them and confirm they pass.** Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Update the docs.**
  - `README.md` lines 56-58. Replace:

```markdown
  left, and a deferred-interest warning when payoff lands after
  `promo_expires_on` (the one optional manual field in the whole system).
```

  With:

```markdown
  left, and a warning when payoff lands after `promo_expires_on` (the one
  optional manual field in the whole system) — interest then applies to the
  remaining balance, back-dated on deferred-interest plans.
```

  - `README.md` line 205. Replace
    `| \`moneta obligations\` | Loans/financing: payment, months left, deferred-interest warnings |`
    with
    `| \`moneta obligations\` | Loans/financing: payment, months left, promo-ends-before-payoff warnings |`.
  - `docs/user-guide.md` lines 457-459. Replace:

```markdown
Loans and 0%-promo financing, fully derived from observed transfers: monthly payment, balance,
estimated months left, and a **deferred interest** warning when the payoff estimate lands after
the promo expiry.
```

  With:

```markdown
Loans and 0%-promo financing, fully derived from observed transfers: monthly payment, balance,
estimated months left, and a red `!` when the payoff estimate lands after the promo expiry:
"payoff lands after the promo ends — interest applies to the remaining balance (back-dated on
deferred-interest plans)". A true 0% promo charges interest only from its end date; a
deferred-interest plan charges it back to the purchase date. Either way the plan won't be paid
off in time. A promo ending today with a balance left warns too. In `--json` and the API the
warning is the `deferred_interest_risk` field — the name is kept for compatibility and means
only "payoff lands after `promo_expires_on`", not that the plan is deferred-interest.
```

  - `docs/user-guide.md` lines 290-291. Replace:

```markdown
   digest, plus any financing obligation newly showing `deferred_interest_risk` (a risk that
   clears re-notifies if it later reappears). Nothing new → nothing sent (no empty pings), but
```

  With:

```markdown
   digest, plus any financing obligation newly showing `deferred_interest_risk` (payoff lands
   after the promo ends; a risk that clears re-notifies if it later reappears). Nothing new →
   nothing sent (no empty pings), but
```
  - `docs/user-guide.md` line 317. Replace
    `(deferred-interest warnings are current-state, so they still send on the first run).`
    with `(promo warnings are current-state, so they still send on the first run).`.
  - `docs/user-guide.md` line 512. Replace
    `promo powers the deferred-interest warning; financing-mode` with
    `promo powers the promo-ends-before-payoff warning; financing-mode`.
  - `docs/PRD.md` line 95. Replace
    `and a deferred-interest warning when payoff lands after \`promo_expires_on\` (the one optional manual field in the system).`
    with
    `and a promo warning when payoff lands after \`promo_expires_on\` (the one optional manual field in the system): interest applies to the remaining balance, back-dated on deferred-interest plans. The API field keeps the name \`deferred_interest_risk\` for compatibility.`
  - `docs/PRD.md` line 122. Replace `A cleared deferred-interest risk drops out` with
    `A cleared promo risk (\`deferred_interest_risk\`) drops out`.
  - `docs/PRD.md`. Insert this row directly below the `|---|---|---|` line under
    `| Date | Release | What shipped |`:

```markdown
| 2026-10-15 | **Neutral promo warning** | `moneta obligations` and the digest no longer call a late payoff "deferred interest", which is false for a plain 0% promo. The summary line reads "payoff lands after the promo ends — interest applies to the remaining balance (back-dated on deferred-interest plans)"; the digest line starts "Promo ends before payoff". The API field keeps its name, `deferred_interest_risk`. |
```

- [ ] **Step 6: Run the gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/cli/main.py src/moneta/pipelines/digest.py src/moneta/views/financing.py tests/test_cli.py tests/test_digest.py tests/test_financing.py README.md docs/user-guide.md docs/PRD.md
git commit -s -m "fix: neutral promo warning — interest applies, not always deferred"
```

---

### Task 3: `accounts --set-promo ID none` clears the date (F5)

**Files:**
- Modify: `src/moneta/cli/main.py:567-568, 590-592`
- Test: `tests/test_cli.py`, `tests/test_api.py`
- Docs: `README.md:208`, `docs/user-guide.md:507, 520-522`, `docs/PRD.md:130`

**Interfaces:**
- Produces nothing new. The CLI sends `PATCH /accounts/{id}` with
  `{"promo_expires_on": null}`, a body the API already handles.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_cli.py`:

```python
@pytest.mark.parametrize(
    ("value", "sent"),
    [("2026-12-31", "2026-12-31"), ("none", None), ("None", None), (" NONE ", None), ("", None)],
)
def test_accounts_set_promo_sends_date_or_clears(
    monkeypatch: pytest.MonkeyPatch, value: str, sent: str | None
) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, json_body))
        if method == "GET":
            return []
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["accounts", "--set-promo", "1", value])
    assert result.exit_code == 0, result.output
    assert ("PATCH", "/accounts/1", {"promo_expires_on": sent}) in calls
    assert "Traceback" not in result.output
```

  Append to `tests/test_api.py`:

```python
async def test_patch_account_null_clears_promo(client: httpx.AsyncClient) -> None:
    await client.post("/sync")
    acct_id = (await client.get("/accounts")).json()[0]["id"]
    await client.patch(f"/accounts/{acct_id}", json={"promo_expires_on": "2026-12-31"})

    r = await client.patch(f"/accounts/{acct_id}", json={"promo_expires_on": None})

    assert r.status_code == 200
    assert (await client.get("/accounts")).json()[0]["promo_expires_on"] is None
    await client.post("/sync")  # a re-sync must not bring the cleared date back
    assert (await client.get("/accounts")).json()[0]["promo_expires_on"] is None
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_cli.py tests/test_api.py -k "set_promo or clears_promo"`.
  Expected: 4 FAIL, the `none`/`None`/` NONE `/empty cases, each exiting 1 with
  `invalid date`. The date case passes. `test_patch_account_null_clears_promo`
  passes already, which confirms the API accepts null and that the cleared choice
  survives a re-sync.

- [ ] **Step 3: Implement.** In `src/moneta/cli/main.py` (`accounts`), replace the
  docstring:

```python
    """List accounts. Flags: --set-type ID TYPE, --set-promo ID YYYY-MM-DD,
    --set-financing ID true|false."""
```

  With:

```python
    """List accounts. Flags: --set-type ID TYPE, --set-promo ID YYYY-MM-DD|none
    (none clears it), --set-financing ID true|false."""
```

  Then replace:

```python
    # Validate --set-promo's date BEFORE firing any PATCH: an invalid date must
    # leave --set-type unapplied too, not partially mutate the account server-side.
    promo = _parse_iso_date(set_promo[1]) if set_promo else None
```

  With:

```python
    # Validate --set-promo's date BEFORE firing any PATCH: an invalid date must
    # leave --set-type unapplied too, not partially mutate the account server-side.
    # `none` (or an empty value) clears the date: the PATCH sends null.
    promo = (
        None
        if set_promo is None or set_promo[1].strip().lower() in {"", "none"}
        else _parse_iso_date(set_promo[1])
    )
```

  The `if set_promo:` branch below already sends `{"promo_expires_on": promo}`.
  Leave it as is.

- [ ] **Step 4: Run them and confirm they pass.** Run the Step 2 command. Expected: PASS.
  Also run `uv run pytest -q -p no:randomly tests/test_cli.py -k set_promo`. The
  two existing invalid-date tests must still pass.

- [ ] **Step 5: Update the docs.**
  - `README.md` line 208. Replace `[--set-promo ID DATE]` with
    `[--set-promo ID DATE\|none]`, and `set shared type / promo expiry / financing metadata`
    with `set shared type / promo expiry (\`none\` clears it) / financing metadata`.
  - `docs/user-guide.md`. After line 507
    (`moneta accounts --set-promo <ID> YYYY-MM-DD        # promo expiry for a financing account`),
    insert:

```text
moneta accounts --set-promo <ID> none              # clear the promo date (an empty value works too)
```

  - `docs/user-guide.md` lines 520-522. Replace:

```markdown
an explicit root choice takes precedence. Clearing financing with `false`, or clearing
the promotion through an account PATCH with `{"promo_expires_on": null}`, remains
effective on subsequent syncs. A promotion-only edit does not answer financing questions.
```

  With:

```markdown
an explicit root choice takes precedence. Clearing financing with `false`, or clearing
the promotion with `--set-promo <ID> none` (an account PATCH with
`{"promo_expires_on": null}`), remains effective on subsequent syncs. A promotion-only edit
does not answer financing questions.
```

  - `docs/PRD.md`. Insert this row directly below the `|---|---|---|` line under
    `| Date | Release | What shipped |`:

```markdown
| 2026-10-15 | **Clear a promo date** | `moneta accounts --set-promo ID none` (case-insensitive, or an empty value) clears the promo expiry: the PATCH sends `null`, which the API already accepted, and the cleared choice survives re-syncs. Any other value must still be `YYYY-MM-DD` and is checked before any request. |
```

- [ ] **Step 6: Run the gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/cli/main.py tests/test_cli.py tests/test_api.py README.md docs/user-guide.md docs/PRD.md
git commit -s -m "feat: accounts --set-promo ID none clears the promo date"
```

---

### Task 4: Upcoming charges include those due today (F7)

**Files:**
- Modify: `src/moneta/views/power.py:107-115, 143`
- Test: `tests/test_power.py:319-337` and new tests
- Docs: `docs/superpowers/specs/2026-07-16-visibility-power-polish-design.md:106`,
  `docs/PRD.md:153` and `:130`, `docs/user-guide.md:338, 340`, `README.md:31-33`

**Interfaces:**
- Produces nothing new. `PowerReport.upcoming` keeps its type, and its window is
  now `[today, month_end]`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_power.py`, in
  `test_upcoming_includes_series_inside_window_excludes_outside`, replace:

```python
    # boundary: next_expected_on == today itself is not "upcoming" (window is exclusive of today)
    await make_series(
        session, merchant="Today Charge", expected_cents=-500, next_expected_on=date(2026, 7, 7)
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert [u.merchant for u in report.upcoming] == ["Rent"]
    assert report.upcoming[0].expected_on == date(2026, 7, 15)
    assert report.upcoming[0].expected_cents == 140000
```

  With:

```python
    # boundaries: due today (not yet charged) and due on the month's last day are both
    # upcoming — the window is [today, month_end]
    await make_series(
        session, merchant="Today Charge", expected_cents=-500, next_expected_on=date(2026, 7, 7)
    )
    await make_series(
        session, merchant="Month End", expected_cents=-2000, next_expected_on=date(2026, 7, 31)
    )
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.upcoming == [
        UpcomingCharge(merchant="Today Charge", expected_on=date(2026, 7, 7), expected_cents=500),
        UpcomingCharge(merchant="Rent", expected_on=date(2026, 7, 15), expected_cents=140000),
        UpcomingCharge(merchant="Month End", expected_on=date(2026, 7, 31), expected_cents=2000),
    ]


async def test_bill_charged_today_is_not_also_upcoming(session: AsyncSession) -> None:
    """Paid today = counted once, as a fixed cost: detection moves the bill's next date
    a period forward, so it never also shows as still to come."""
    checking = await make_account(session, type=AccountType.checking)
    for month in (5, 6, 7):
        await make_txn(
            session,
            checking,
            amount_cents=-4999,
            merchant="Corner Gym",
            posted_on=date(2026, month, 7),
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 7))
    report = await power_report(session, today=date(2026, 7, 7))
    assert [line.merchant for line in report.fixed_costs] == ["Corner Gym"]
    assert report.upcoming == []
```

  Then insert this test directly above `test_upcoming_sorted_by_expected_on`:

```python
async def test_upcoming_loan_payment_due_today_but_not_one_paid_today(
    session: AsyncSession,
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    due = await make_account(session, type=AccountType.loan, name="Acme Car Loan")
    paid = await make_account(session, type=AccountType.loan, name="Acme Boat Loan")
    for loan, day in ((due, date(2026, 6, 7)), (paid, date(2026, 6, 7)), (paid, date(2026, 7, 7))):
        out = await make_txn(
            session, checking, amount_cents=-13500, merchant="Acme Bank", posted_on=day
        )
        inn = await make_txn(session, loan, amount_cents=13500, merchant="Acme Bank", posted_on=day)
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.upcoming == [
        UpcomingCharge(
            merchant="Acme Car Loan — payment", expected_on=date(2026, 7, 7), expected_cents=13500
        )
    ]
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_power.py`.
  Expected: 2 FAIL: `test_upcoming_includes_series_inside_window_excludes_outside`
  (`Today Charge` missing) and
  `test_upcoming_loan_payment_due_today_but_not_one_paid_today` (empty list).
  `test_bill_charged_today_is_not_also_upcoming` passes already. It pins the
  no-double-count check that this task relies on.

- [ ] **Step 3: Implement.** In `src/moneta/views/power.py`, replace:

```python
        for s in fixed_series
        if today < s.next_expected_on <= month_end
    ]
```

  With:

```python
        for s in fixed_series
        # due today counts: a bill charged today has already moved next_expected_on a
        # period forward (detect_recurring), so only unpaid ones land here
        if today <= s.next_expected_on <= month_end
    ]
```

  Then replace `            if today < projected <= month_end:` with
  `            if today <= projected <= month_end:`.

- [ ] **Step 4: Run them and confirm they pass.** Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Update the docs.**
  - The spec (`docs/superpowers/specs/2026-07-16-visibility-power-polish-design.md`,
    line 106). Replace
    ``non-cc-payment outflow series with `next_expected_on` in `(today, month_end]`,``
    with:

```markdown
non-cc-payment outflow series with `next_expected_on` in `[today, month_end]`
(amended 2026-10-15 from `(today, month_end]`: a bill due today and not yet charged
is still to come, and a synced charge moves `next_expected_on` a period forward, so a
paid bill never shows twice),
```

  - `docs/PRD.md` line 153. Replace
    ``with `next_expected_on` in `(today, month_end]`, plus derived loan payments``
    with
    ``with `next_expected_on` in `[today, month_end]` (today was excluded until 2026-10-15), plus derived loan payments``.
  - `docs/PRD.md`. Insert this row directly below the `|---|---|---|` line under
    `| Date | Release | What shipped |`:

```markdown
| 2026-10-15 | **Upcoming includes today** | `PowerReport.upcoming` covers `[today, month_end]` for series and derived loan payments alike: a bill due today and not yet charged is listed. Once its charge syncs, detection moves `next_expected_on` a period forward (a loan's projected date moves the same way), so a bill paid today is never also upcoming. |
```

  - `docs/user-guide.md` line 338. Replace
    `fixed costs (plus derived loan payments) due later this month, e.g.` with
    `fixed costs (plus derived loan payments) due from today through month-end, e.g.`.
    Then, on line 340, replace `  is due before month-end.` with
    `  is due by month-end. A bill due today shows until its charge syncs.`.
  - `README.md` lines 32-33. Replace:

```markdown
  plus spent-so-far, remaining, safe-to-spend per day, and upcoming charges due later
  this month, for the current month. Accrual vs. cash-out accounting keeps "spent so
```

  With:

```markdown
  plus spent-so-far, remaining, safe-to-spend per day, and upcoming charges due from
  today through month-end, for the current month. Accrual vs. cash-out accounting keeps "spent so
```

- [ ] **Step 6: Run the gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/views/power.py tests/test_power.py docs/superpowers/specs/2026-07-16-visibility-power-polish-design.md docs/PRD.md docs/user-guide.md README.md
git commit -s -m "fix: upcoming charges include ones due today"
```

---

### Task 5: An unanswered financing question counts the card as financing (F2)

**Files:**
- Modify: `src/moneta/account_metadata.py` (append), `src/moneta/logical.py:9, 14-29, 114-147`,
  `src/moneta/queries.py:144-145`, `src/moneta/views/financing.py:9, 27-33`
- Test: `tests/test_power.py`, `tests/test_financing.py`, `tests/test_queries.py`,
  `tests/test_recurring.py`
- Docs: `CLAUDE.md:43`, `docs/user-guide.md:477-479, 487-489`, `docs/PRD.md:66, 95` and `:130`

**Interfaces:**
- Consumes: `apply_resolution(session, item, resolution, resolved_by="manual")`
  (`pipelines/review.py:296`). With `{"financing": False}` it sets the root's
  `financing_mode = False` and resolves the item.
- Produces:
  - `account_metadata.open_financing_roots(items: list[ReviewItem], roots: dict[int, int], decisions: AccountDecisions) -> set[int]`,
    which returns logical-account root IDs.
  - `LogicalAccount.financing_pending: bool`, a new required dataclass field
    placed last. `load_logical_data` is its only constructor.
  - `LogicalAccount.loan_like -> bool` (a property),
    `type == AccountType.loan or financing_mode or financing_pending`.
  - `ClassifiedLink.inflow_is_loan_like` now equals the inflow account's `loan_like`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_power.py`, replace the
  import block (lines 1-9) with:

```python
from calendar import monthrange
from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    Account,
    AccountType,
    Cadence,
    Direction,
    ReviewItem,
    ReviewKind,
    SeriesStatus,
    SyncRun,
    TransferLink,
)
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.review import apply_resolution
from moneta.views.power import SeriesLine, UpcomingCharge, power_report
from tests.factories import make_account, make_series, make_txn
```

  Then append to `tests/test_power.py`:

```python
async def _card_paid_monthly(
    session: AsyncSession, series_id: int | None = None, **card: Any
) -> Account:
    """A credit card owing $900.00, paid $100.00 from checking on May, June and July 5."""
    checking = await make_account(session, type=AccountType.checking)
    account = await make_account(
        session, type=AccountType.credit, name="Acme Store Card", balance_cents=-90000, **card
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-10000,
            merchant="Acme Card Payment",
            posted_on=date(2026, month, 5),
            series_id=series_id,
        )
        inn = await make_txn(
            session,
            account,
            amount_cents=10000,
            merchant="Acme Card Payment",
            posted_on=date(2026, month, 5),
        )
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
    await session.flush()
    return account


def _financing_question(account: Account) -> ReviewItem:
    return ReviewItem(
        kind=ReviewKind.financing_account,
        question="'Acme Store Card' looks like promo financing being paid down?",
        payload={"account_id": account.id},
    )


_CARD_PAYMENT_LINE = SeriesLine(
    merchant="Acme Store Card — payment",
    cadence=Cadence.monthly,
    monthly_cents=10000,
    expected_cents=10000,
)


@pytest.mark.parametrize("state", ["confirmed", "open question"])
async def test_open_financing_question_counts_payments_as_fixed(
    session: AsyncSession, state: str
) -> None:
    """No one has answered yet (the LLM never does): the card counts exactly as a
    confirmed financing card does."""
    card = await _card_paid_monthly(session, financing_mode=state == "confirmed")
    if state == "open question":
        session.add(_financing_question(card))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == [_CARD_PAYMENT_LINE]
    assert report.total_fixed_cents == 10000


async def test_resolving_not_financing_makes_it_a_revolving_card_again(
    session: AsyncSession,
) -> None:
    card = await _card_paid_monthly(session)
    item = _financing_question(card)
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"financing": False})
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == []  # a plain card's payments are never fixed costs
    assert report.spent_so_far_cents == 0  # nor spend: a card payment is a transfer


async def test_open_financing_question_with_old_payment_series_counts_once(
    session: AsyncSession,
) -> None:
    """A payment series formed before links excluded it stays the one line — no derived
    payment line on top of it."""
    series = await make_series(session, merchant="Acme Card Payment", expected_cents=-10000)
    card = await _card_paid_monthly(session, series_id=series.id)
    session.add(_financing_question(card))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert [line.merchant for line in report.fixed_costs] == ["Acme Card Payment"]
    assert report.total_fixed_cents == 10000


async def test_financing_ruling_beats_an_open_question_on_an_alias(
    session: AsyncSession,
) -> None:
    """Ruled 'not financing', then the same card reappears from a second provider with
    its own open question: the human's ruling stands."""
    card = await _card_paid_monthly(session)
    alias = await make_account(
        session,
        type=AccountType.credit,
        name="Acme Store Card",
        balance_cents=-90000,
        canonical_account_id=card.id,
        source="second",
    )
    ruled = _financing_question(card)
    session.add(ruled)
    await session.flush()
    await apply_resolution(session, ruled, {"financing": False})
    session.add(_financing_question(alias))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == []
```

  In `tests/test_financing.py`, change the models import to
  `from moneta.models import AccountType, ReviewItem, ReviewKind, Transaction, TransferLink`.
  Then append:

```python
async def test_open_financing_question_gets_obligation(session: AsyncSession) -> None:
    """Until someone answers the financing question, the card is an obligation."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(
        session, type=AccountType.credit, name="Acme Store Card", balance_cents=-90000
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-30000,
            merchant="Acme Card Payment",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(
            session,
            card,
            amount_cents=30000,
            merchant="Acme Card Payment",
            posted_on=date(2026, month, 5),
        )
        await _link(session, out, inn)
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="'Acme Store Card' looks like promo financing being paid down?",
            payload={"account_id": card.id},
        )
    )
    await session.flush()

    obs = await compute_obligations(session, today=date(2026, 7, 7))

    assert [(ob.account_id, ob.monthly_payment_cents, ob.months_left) for ob in obs] == [
        (card.id, 30000, 3)
    ]
```

  In `tests/test_queries.py`, change the models import to
  `from moneta.models import Account, AccountType, Cadence, ReviewItem, ReviewKind, TransferLink`.
  Then append:

```python
async def test_classified_links_open_financing_question_is_loan_like(
    session: AsyncSession,
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(session, type=AccountType.credit, balance_cents=-90000)
    out = await make_txn(session, checking, amount_cents=-10000, posted_on=date(2026, 7, 1))
    inn = await make_txn(session, card, amount_cents=10000, posted_on=date(2026, 7, 1))
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="q",
            payload={"account_id": card.id},
        )
    )
    await session.flush()

    [link] = await classified_links(session)

    assert link.inflow_is_loan_like is True
```

  In `tests/test_recurring.py`, insert this test directly above
  `test_partial_untag_keeps_series`. Every name it uses is already imported there.

```python
async def test_open_financing_question_untags_old_card_payment_series(
    session: AsyncSession,
) -> None:
    """The sync that opens a financing question already treats the card as financing:
    an old payment series over it is untagged and ended now, not after the answer."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(session, type=AccountType.credit, balance_cents=-90000)
    series = await make_series(
        session,
        merchant="Acme Card Payment",
        expected_cents=-10000,
        next_expected_on=date(2026, 7, 5),
    )
    outflow_ids: list[int] = []
    for month in (4, 5, 6):
        out = await make_txn(
            session,
            checking,
            amount_cents=-10000,
            merchant="Acme Card Payment",
            posted_on=date(2026, month, 5),
            series_id=series.id,
        )
        inn = await make_txn(session, card, amount_cents=10000, posted_on=date(2026, month, 5))
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
        outflow_ids.append(out.id)
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="q",
            payload={"account_id": card.id},
        )
    )
    await session.flush()
    await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert series.status == SeriesStatus.ended
    untagged = (
        (await session.execute(select(Transaction).where(Transaction.id.in_(outflow_ids))))
        .scalars()
        .all()
    )
    assert all(t.series_id is None for t in untagged)
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_power.py tests/test_financing.py tests/test_queries.py tests/test_recurring.py`.
  Expected: 5 FAIL:
  - `test_open_financing_question_counts_payments_as_fixed[open question]`
  - `test_open_financing_question_with_old_payment_series_counts_once` (total 0)
  - `test_open_financing_question_gets_obligation` (empty list)
  - `test_classified_links_open_financing_question_is_loan_like`
  - `test_open_financing_question_untags_old_card_payment_series` (series still
    `active`)

  `…resolving_not_financing…`, `…counts_payments_as_fixed[confirmed]` and
  `…ruling_beats…alias` pass already. They pin behavior this task must keep.

- [ ] **Step 3: Implement.** Append to `src/moneta/account_metadata.py`:

```python
def open_financing_roots(
    items: list[ReviewItem], roots: dict[int, int], decisions: AccountDecisions
) -> set[int]:
    """Logical accounts with an open financing question that no one has ruled on.

    The LLM never answers financing questions, so until a human does, the account is
    treated as financing. A ruling recorded on any member of the logical account wins
    over a question still open on another member.
    """
    ruled = {roots[account_id] for account_id in decisions.financing if account_id in roots}
    pending: set[int] = set()
    for item in items:
        if item.kind != ReviewKind.financing_account or item.status != ReviewStatus.open:
            continue
        account_id = item.payload.get("account_id")
        if type(account_id) is int and account_id in roots:
            pending.add(roots[account_id])
    return pending - ruled
```

  In `src/moneta/logical.py`, replace the import
  `from moneta.account_metadata import account_decisions, effective_account_metadata`
  with:

```python
from moneta.account_metadata import (
    account_decisions,
    effective_account_metadata,
    open_financing_roots,
)
```

  In `LogicalAccount`, replace:

```python
    source: str
    mask: str | None
    balance_account_id: int


@dataclass(frozen=True)
class LogicalHolding:
```

  With:

```python
    source: str
    mask: str | None
    balance_account_id: int
    financing_pending: bool  # an open financing question no one has ruled on yet

    @property
    def loan_like(self) -> bool:
        """Loan semantics: a loan, a financing card, or a card whose financing question
        is still open (the LLM never answers those, so they count until a human rules)."""
        return self.type == AccountType.loan or self.financing_mode or self.financing_pending


@dataclass(frozen=True)
class LogicalHolding:
```

  In `load_logical_data`, replace:

```python
        decisions = account_decisions(list(await session.scalars(select(ReviewItem))))

    account_roots = resolve_account_roots(accounts)
```

  With:

```python
        review_items = list(await session.scalars(select(ReviewItem)))
        decisions = account_decisions(review_items)

    account_roots = resolve_account_roots(accounts)
    pending_financing = open_financing_roots(review_items, account_roots, decisions)
```

  In the same function, replace:

```python
            balance_account_id=latest.id,
        )
```

  With:

```python
            balance_account_id=latest.id,
            financing_pending=root_id in pending_financing,
        )
```

  In `src/moneta/queries.py` (`classified_links`), replace:

```python
                inflow_is_loan_like=in_account.type == AccountType.loan
                or in_account.financing_mode,
```

  With:

```python
                inflow_is_loan_like=in_account.loan_like,
```

  In `src/moneta/views/financing.py`, delete the line `from moneta.models import AccountType`.
  Then replace:

```python
        if (a.type == AccountType.loan or a.financing_mode)
        and a.balance_cents != 0
        and a.currency == primary
```

  With:

```python
        if a.loan_like and a.balance_cents != 0 and a.currency == primary
```

- [ ] **Step 4: Run them and confirm they pass.** Run the Step 2 command. Expected: PASS.
  Then run `uv run pytest -q -p no:randomly tests/test_transactions.py tests/test_financing_detect.py tests/test_logical.py tests/test_account_overlap.py`.
  It must also pass, because those tests build `LogicalAccount`s and financing
  questions.

- [ ] **Step 5: Update the docs.**
  - `CLAUDE.md` line 43 (Transfer-link semantics). Replace
    `Loan-like payments (account \`type == loan\` or \`financing_mode\`) are instead derived`
    with
    `Loan-like payments (\`LogicalAccount.loan_like\`: account \`type == loan\`, \`financing_mode\`, or an open \`financing_account\` question nobody has ruled on — the LLM never answers those, and a ruling on any member of the logical account beats an open question) are instead derived`.
  - `docs/user-guide.md` lines 477-479. Replace:

```markdown
the answer is remembered and never asked again for that account. Answering `y` sets
`financing_mode`, which gives the account loan semantics: its payments count as fixed
costs (instead of its purchases), and it appears in `moneta obligations`.
```

  With:

```markdown
the answer is remembered and never asked again for that account. Answering `y` sets
`financing_mode`, which gives the account loan semantics: its payments count as fixed
costs (instead of its purchases), and it appears in `moneta obligations`. Until you
answer, moneta already treats the card this way — the auto-review LLM never answers this
question, and a card that fits the fingerprint shouldn't drop out of fixed costs while it
waits. Answering `n` makes it a plain credit card again. If you already answered for this
card through another provider's record of it, that answer stands and a question still
open on the other record changes nothing.
```

  (The words "instead of its purchases" stay. Until Task 6 the code still counts
  those purchases as spend; Task 6 makes the code match the guide.)

  - `docs/user-guide.md` lines 487-489. Replace:

```markdown
After confirming financing this way (review answer or `--set-financing`), any old
credit-payment series over that card is replaced by the derived per-account payment line
on the next `moneta sync`
```

  With:

```markdown
Once a card counts as financing — an open question, a `y` answer, or `--set-financing
true` — any old credit-payment series over it is replaced by the derived per-account
payment line on the next `moneta sync`
```

  - `docs/PRD.md` line 66. Replace
    `A confirmed account gets loan semantics: its payments count as fixed costs and it appears in \`obligations\`.`
    with
    `A confirmed account gets loan semantics: its payments count as fixed costs and it appears in \`obligations\`. An account whose question is still open gets the same semantics until a human answers (the LLM never answers it), and a ruling on any of its provider records beats a question open on another; answering no makes it a plain card again.`
  - `docs/PRD.md` line 95. Replace
    `any \`loan\`-type or financing-mode account with a payment` with
    `any \`loan\`-type or financing-mode account (or a card whose financing question is still unanswered) with a payment`.
  - `docs/PRD.md`. Insert this row directly below the `|---|---|---|` line under
    `| Date | Release | What shipped |`:

```markdown
| 2026-10-15 | **Unanswered financing questions count as financing** | A card with an open `financing_account` question (the LLM never answers these) gets financing semantics until a human rules: its payments become a fixed-cost line and an `obligations` row, exactly as for a confirmed card, and the sync that opens the question already retires an old payment series over it. Answering no makes it a plain card again; a ruling on any of the card's provider records beats a question still open on another. |
```

- [ ] **Step 6: Run the gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/account_metadata.py src/moneta/logical.py src/moneta/queries.py src/moneta/views/financing.py tests/test_power.py tests/test_financing.py tests/test_queries.py tests/test_recurring.py CLAUDE.md docs/user-guide.md docs/PRD.md
git commit -s -m "fix: an unanswered financing question counts the card as financing"
```

---

### Task 6: A financing card's purchases are not also spend (F2, continued)

**Files:**
- Modify: `src/moneta/views/transactions.py:55-107`, `src/moneta/views/power.py:172-175`,
  `src/moneta/views/cashflow.py:31-39, 94-101`
- Test: `tests/test_power.py`, `tests/test_transactions.py`, `tests/test_cashflow.py`
- Docs: `docs/user-guide.md:332-333, 372`, `README.md:38`, `docs/PRD.md:66, 91, 94` and
  `:130`, `CLAUDE.md:43`, the 2026-07-16 visibility spec (lines 71-72)

**Interfaces:**
- Consumes:
  - `LogicalAccount.loan_like` (Task 5).
  - The Task 5 helpers in `tests/test_power.py`: `_financing_question(account) -> ReviewItem`
    and `_CARD_PAYMENT_LINE`, the `Acme Store Card — payment` line at $100.00 monthly.
- Produces:
  - `spend_reason(txn_amount_cents, account_type, account_loan_like, currency, primary, link, series_status, series_discretionary) -> str | None`.
    The new third positional parameter is `account_loan_like: bool`.
  - A new reason string, `"financed purchase"`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_power.py`, after the
  Task 5 tests, so that `_CARD_PAYMENT_LINE` and `_financing_question` are already
  defined above it:

```python
@pytest.mark.parametrize(
    ("state", "fixed", "spent"),
    [
        ("financing mode", [_CARD_PAYMENT_LINE], 0),
        ("open question", [_CARD_PAYMENT_LINE], 0),
        ("plain card", [], 100000),
    ],
)
async def test_financed_purchase_is_not_also_spend(
    session: AsyncSession, state: str, fixed: list[SeriesLine], spent: int
) -> None:
    """A $1,000.00 financed purchase is paid off by the card's $100.00/mo payments, and
    those are the fixed cost: counting the purchase as spend too would count it twice.
    A plain card's purchase is spend, and its payment is a transfer."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(
        session,
        type=AccountType.credit,
        name="Acme Store Card",
        balance_cents=-90000,
        financing_mode=state == "financing mode",
    )
    if state == "open question":
        session.add(_financing_question(card))
    await make_txn(
        session, card, amount_cents=-100000, merchant="Acme Furniture", posted_on=date(2026, 7, 2)
    )
    out = await make_txn(
        session,
        checking,
        amount_cents=-10000,
        merchant="Acme Card Payment",
        posted_on=date(2026, 7, 5),
    )
    inn = await make_txn(
        session, card, amount_cents=10000, merchant="Acme Card Payment", posted_on=date(2026, 7, 5)
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.fixed_costs == fixed
    assert report.spent_so_far_cents == spent
```

  In `tests/test_transactions.py`, replace
  `from moneta.models import AccountType, SeriesStatus, TransferLink` with
  `from moneta.models import AccountType, ReviewItem, ReviewKind, SeriesStatus, TransferLink`.
  Then append:

```python
async def test_financed_purchase_reason(session: AsyncSession) -> None:
    """A loan-like card's own purchase is paid off by its payments (the fixed cost), so it
    isn't spend; a plain card's purchase is, and a loan account keeps its own reason."""
    financing = await make_account(session, type=AccountType.credit, financing_mode=True)
    pending = await make_account(session, type=AccountType.credit)
    plain = await make_account(session, type=AccountType.credit)
    loan = await make_account(session, type=AccountType.loan)
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account, question="q", payload={"account_id": pending.id}
        )
    )
    for account, merchant in (
        (financing, "Acme Furniture"),
        (pending, "Acme Mattress"),
        (plain, "Corner Curry"),
        (loan, "Acme Loan Fee"),
    ):
        await make_txn(
            session, account, amount_cents=-100000, merchant=merchant, posted_on=date(2026, 7, 2)
        )
    await session.flush()
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    assert {r.merchant: (r.counted_in_spend, r.excluded_because) for r in rows} == {
        "Acme Furniture": (False, "financed purchase"),
        "Acme Mattress": (False, "financed purchase"),
        "Corner Curry": (True, None),
        "Acme Loan Fee": (False, "non-spend account"),
    }
```

  In `tests/test_cashflow.py`, replace:

```python
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.cadence import month_bounds
from moneta.models import Account, AccountType, TransferLink
```

  With:

```python
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.cadence import month_bounds
from moneta.models import Account, AccountType, ReviewItem, ReviewKind, TransferLink
```

  Then append:

```python
@pytest.mark.parametrize(
    ("state", "spend"), [("financing mode", 0), ("open question", 0), ("plain card", 100000)]
)
async def test_accrual_excludes_financed_purchase(
    session: AsyncSession, state: str, spend: int
) -> None:
    """History and `cashflow` agree with `power`: a financing card's purchase is paid by
    its fixed-cost payments, so it is not spend; a plain card's purchase still is."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(
        session, type=AccountType.credit, financing_mode=state == "financing mode"
    )
    if state == "open question":
        session.add(
            ReviewItem(
                kind=ReviewKind.financing_account, question="q", payload={"account_id": card.id}
            )
        )
    await make_txn(
        session,
        card,
        amount_cents=-100000,
        posted_on=date(2026, 7, 2),
        description="ACME FURNITURE",
    )
    out = await make_txn(
        session, checking, amount_cents=-10000, posted_on=date(2026, 7, 5), description="ACME PMT"
    )
    inn = await make_txn(
        session, card, amount_cents=10000, posted_on=date(2026, 7, 5), description="ACME PMT"
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    assert await accrual_spend(session, date(2026, 7, 1), date(2026, 7, 31)) == spend
    assert await accrual_by_month(session, months=1, today=date(2026, 7, 20)) == [
        ("2026-07", 0, spend)
    ]
```

  If branch `fix-transfer-scoring` has already merged, `accrual_by_month` returns
  four-field `MonthFlows` rows. Write the last expectation as
  `[("2026-07", 0, 0, spend)]` instead (month, income, other inflows, spend).

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_power.py tests/test_transactions.py tests/test_cashflow.py`.
  Expected: 5 FAIL:
  - `test_financed_purchase_is_not_also_spend[financing mode-…]` and
    `[open question-…]` (`assert 100000 == 0` on `spent_so_far_cents`)
  - `test_financed_purchase_reason` (both financed rows counted, reason `None`)
  - `test_accrual_excludes_financed_purchase[financing mode-0]` and
    `[open question-0]` (`assert 100000 == 0`)

  The plain-card cases pass already. They pin that a plain card's purchase still
  counts.

- [ ] **Step 3: Implement.** In `src/moneta/views/transactions.py`, make four
  changes.

  First, replace:

```python
def spend_reason(
    txn_amount_cents: int,
    account_type: str,
    currency: str,
```

  With:

```python
def spend_reason(
    txn_amount_cents: int,
    account_type: str,
    account_loan_like: bool,
    currency: str,
```

  Second, in its docstring, replace:

```python
    the two views can never quietly diverge on what counts as spend."""
```

  With:

```python
    the two views can never quietly diverge on what counts as spend.
    `account_loan_like` is the txn's logical account's `loan_like`: that account's own
    outflows (purchases, interest, fees) are paid off by the payments `power` counts as
    its fixed cost, so counting them as spend too would count them twice."""
```

  Third, replace:

```python
    if account_type not in SPEND_ACCOUNT_TYPES:
        return "non-spend account"
    if currency != primary:
```

  With:

```python
    if account_type not in SPEND_ACCOUNT_TYPES:
        return "non-spend account"
    if account_loan_like:
        return "financed purchase"
    if currency != primary:
```

  Fourth, in `_excluded_because`, replace:

```python
    reason = spend_reason(
        txn.amount_cents,
        account.type,
        account.currency,
```

  With:

```python
    reason = spend_reason(
        txn.amount_cents,
        account.type,
        account.loan_like,
        account.currency,
```

  In `src/moneta/views/power.py` (spent-so-far), replace:

```python
        if spend_reason(
            txn.amount_cents,
            account.type,
            account.currency,
```

  With:

```python
        if spend_reason(
            txn.amount_cents,
            account.type,
            account.loan_like,
            account.currency,
```

  In `src/moneta/views/cashflow.py` (`_accrual`), replace:

```python
    logical = await load_logical_data(session)
    txns = [
        t
        for t in logical.transactions
        if start <= t.posted_on <= end
        and logical.accounts[logical.account_roots[t.account_id]].type in SPEND_ACCOUNT_TYPES
        and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        and (t.amount_cents > 0 if inflows else t.amount_cents < 0)
    ]
```

  With:

```python
    logical = await load_logical_data(session)
    # a loan-like account's own outflows are paid off by the payments `power` counts as
    # its fixed cost, so they are not spend (spend_reason's "financed purchase")
    financed = {account_id for account_id, a in logical.accounts.items() if a.loan_like}
    txns = [
        t
        for t in logical.transactions
        if start <= t.posted_on <= end
        and logical.accounts[logical.account_roots[t.account_id]].type in SPEND_ACCOUNT_TYPES
        and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        and (t.amount_cents > 0 if inflows else t.amount_cents < 0)
        and not (t.amount_cents < 0 and logical.account_roots[t.account_id] in financed)
    ]
```

  In `accrual_by_month`, replace:

```python
    logical = await load_logical_data(session)
    txns = [
        t
        for t in logical.transactions
        if oldest_start <= t.posted_on <= newest_end
        and logical.accounts[logical.account_roots[t.account_id]].type in SPEND_ACCOUNT_TYPES
        and logical.accounts[logical.account_roots[t.account_id]].currency == primary
    ]
```

  With:

```python
    logical = await load_logical_data(session)
    # a loan-like account's own outflows are paid off by the payments `power` counts as
    # its fixed cost, so they are not spend (spend_reason's "financed purchase")
    financed = {account_id for account_id, a in logical.accounts.items() if a.loan_like}
    txns = [
        t
        for t in logical.transactions
        if oldest_start <= t.posted_on <= newest_end
        and logical.accounts[logical.account_roots[t.account_id]].type in SPEND_ACCOUNT_TYPES
        and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        and not (t.amount_cents < 0 and logical.account_roots[t.account_id] in financed)
    ]
```

  Leave `accrual_by_month`'s docstring alone, since branch `fix-transfer-scoring`
  replaces it whole (see Background). If that branch merged first, the `txns`
  block is unchanged there, so the same Replace/With applies.

- [ ] **Step 4: Run them and confirm they pass.** Run the Step 2 command.
  Expected: PASS (66 tests).

- [ ] **Step 5: Update the docs.**
  - `docs/user-guide.md`. In the `moneta txns` reason table, directly after the
    `| \`non-spend account\` | … |` row (line 372), insert:

```markdown
| `financed purchase` | A purchase, interest charge or fee on a financing card — `financing_mode`, or a card whose financing question is still open. Its payments count as a fixed cost in `power` instead, so the purchase isn't counted twice. Known gap: a new financed purchase shows in no `power` figure until the card's first payment is linked (`moneta obligations` lists the balance meanwhile) |
```

  - `docs/user-guide.md` lines 332-333. Replace:

```markdown
- **Spent so far / remaining** — discretionary spend this month (accrual: credit purchases
  count when made, not when the card is paid).
```

  With:

```markdown
- **Spent so far / remaining** — discretionary spend this month (accrual: credit purchases
  count when made, not when the card is paid). A financing card's purchases don't count:
  its payments are a fixed cost instead (`financed purchase` in `moneta txns`).
```

  - `README.md` line 38. Replace
    `payment, credit-card payment, fixed-cost series, ...) when it isn't.` with
    `payment, credit-card payment, financed purchase, fixed-cost series, ...) when it isn't.`.
  - `docs/PRD.md` line 66. Replace
    `its payments count as fixed costs and it appears in \`obligations\`` with
    `its payments count as fixed costs (instead of its purchases, which are not spend) and it appears in \`obligations\``.
  - `docs/PRD.md` line 91. Replace
    `Credit-card payment series are excluded from fixed costs (their purchases are counted instead — no double count).`
    with
    `Credit-card payment series are excluded from fixed costs (their purchases are counted instead — no double count). A financing card is the reverse: its payments are fixed costs and its own purchases are not spend.`
  - `docs/PRD.md` line 94. Replace
    `non-spend account, or foreign currency, matching power's spent-so-far rules.` with
    `non-spend account, financed purchase, or foreign currency, matching power's spent-so-far rules.`
  - `docs/PRD.md`. Insert this row directly below the `|---|---|---|` line under
    `| Date | Release | What shipped |`:

```markdown
| 2026-10-15 | **Financed purchases aren't spent twice** | A loan-like account's own outflows — purchases, interest and fees on a financing card, or on a card whose financing question is still open — no longer count as spend: its payments are already the fixed cost. `moneta txns` labels them `financed purchase`, and `power`'s spent-so-far, `power --history` and `cashflow`'s accrual spend all leave them out. Known gap: a new financed purchase shows in no `power` figure until the card's first payment is linked; `obligations` lists the balance meanwhile. |
```

  - `CLAUDE.md` line 43 (Transfer-link semantics). Replace
    `surfaced as fixed-cost lines in \`power\` / rows in \`obligations\` directly — not via detection.`
    with
    `surfaced as fixed-cost lines in \`power\` / rows in \`obligations\` directly — not via detection. A loan-like account's own outflows (purchases, interest, fees) are therefore never spend: \`spend_reason\` gives them \`financed purchase\`, and \`views/cashflow.py\`'s two accrual filters (its own copy of the spend rule) drop them the same way — keep the copies in step.`
  - In the spec (`docs/superpowers/specs/2026-07-16-visibility-power-polish-design.md`,
    lines 71-72), replace:

```markdown
("inflow", "transfer", "loan payment", "credit-card payment", "fixed cost
(series X)", "non-spend account", "foreign currency"). Endpoint:
```

  With:

```markdown
("inflow", "transfer", "loan payment", "credit-card payment", "fixed cost
(series X)", "non-spend account", "financed purchase", "foreign currency";
"financed purchase", added 2026-10-15, is an outflow on a loan-like account,
whose payments are the fixed cost). Endpoint:
```

- [ ] **Step 6: Run the gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/views/transactions.py src/moneta/views/power.py src/moneta/views/cashflow.py tests/test_power.py tests/test_transactions.py tests/test_cashflow.py docs/user-guide.md README.md docs/PRD.md CLAUDE.md docs/superpowers/specs/2026-07-16-visibility-power-polish-design.md
git commit -s -m "fix: a financing card's purchases are not also spend"
```
