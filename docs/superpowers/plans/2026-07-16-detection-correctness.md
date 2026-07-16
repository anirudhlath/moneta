# Detection Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three-way bill/habit/not-recurring classification, deterministic financing-mode detection, per-loan-account payment derivation from transfer links, cadence-fallback tolerance, overrule CLI, and correct spend bucketing.

**Architecture:** Schema first (two flags, migration 0003), then a pure `cadence.py` module (breaks the queries→recurring import cycle), then the `ClassifiedLink`/derivation plumbing, then the pipeline/view/CLI changes task-by-task, suite green after each. Spec: `docs/superpowers/specs/2026-07-16-detection-correctness-design.md` — read it; acceptance criteria live there.

**Tech Stack:** FastAPI + Pydantic v2, SQLAlchemy async + Alembic, typer/rich, pytest.

## Global Constraints

- Gate before every commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — all pass, output pristine (a warning is a failure).
- Money integer cents (`*_cents`), negative = outflow. API responses per the cents convention; CLI formats via `fmt_money`.
- Enum columns load as plain `str` — compare with `==`, never `is`.
- Pipelines commit; views don't. `cli/` zero business logic. LLM classifies, never supplies money values; no LLM → degrade to ReviewItem.
- Schema change = Alembic revision + `_HEAD` bump in `db.py` (tests/test_migrations.py pins parity).
- Branch `feature/detection-correctness` off main.
- LLM answer validation: malformed/unexpected values are treated as "no answer" (degrade), never coerced.

---

### Task 0: Branch

- [ ] `git checkout -b feature/detection-correctness main`

---

### Task 1: Schema — `discretionary` + `financing_mode` + `ReviewKind.financing_account`

**Files:**
- Modify: `src/moneta/models.py` (Account ~line 104, RecurringSeries ~line 143, ReviewKind ~line 93)
- Create: `src/moneta/migrations/versions/0003_discretionary_and_financing.py`
- Modify: `src/moneta/db.py` (`_HEAD` line 16)
- Test: `tests/test_migrations.py` (existing parity tests cover it; add none)

**Interfaces:**
- Produces: `Account.financing_mode: Mapped[bool]` (default False), `RecurringSeries.discretionary: Mapped[bool]` (default False), `ReviewKind.financing_account`.

- [ ] **Step 1: Models.** Add to `Account`: `financing_mode: Mapped[bool] = mapped_column(default=False)` (after `promo_expires_on`). Add to `RecurringSeries`: `discretionary: Mapped[bool] = mapped_column(default=False)` (after `status`). Add `financing_account = "financing_account"` to `ReviewKind`.

- [ ] **Step 2: Migration** — `0003_discretionary_and_financing.py`, modeled on `0002_sync_runs.py`:

```python
"""Add recurring_series.discretionary and accounts.financing_mode flags."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recurring_series",
        sa.Column("discretionary", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "accounts",
        sa.Column("financing_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("accounts", "financing_mode")
    op.drop_column("recurring_series", "discretionary")
```

- [ ] **Step 3: `db.py`** — `_HEAD = "0003"`.

- [ ] **Step 4: Run the migration parity tests first** (`uv run pytest tests/test_migrations.py -q` — they fail before Steps 1–3 are all in, pass after), then the full gate.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(schema): discretionary series + financing-mode accounts (migration 0003)"`

---

### Task 2: `cadence.py` — pure cadence math module

**Files:**
- Create: `src/moneta/cadence.py`
- Modify: `src/moneta/pipelines/recurring.py` (delete the moved code; import from `moneta.cadence`)

**Interfaces:**
- Produces: `moneta.cadence` exporting `CADENCE_DAYS`, `TOLERANCE`, `GRACE_DAYS`, `PER_MONTH`, `advance_expected_on(d, cadence)`, `match_cadence(dates) -> tuple[Cadence, date] | None`, `monthlyize(expected_cents: int, cadence: Cadence) -> int`.
- `recurring.py` re-imports and keeps its public names (`CADENCE_DAYS`, `GRACE_DAYS`, `advance_expected_on`, `monthly_cents`) so `events.py`, `views/power.py`, `views/financing.py` importers are untouched. `_TOLERANCE`/`_PER_MONTH`/`_match_cadence` call sites inside recurring.py switch to the new names.

- [ ] **Step 1:** Move from `recurring.py` into new `cadence.py` verbatim (renaming `_TOLERANCE`→`TOLERANCE`, `_PER_MONTH`→`PER_MONTH`, `_match_cadence`→`match_cadence`): `CADENCE_DAYS`, `TOLERANCE`, `GRACE_DAYS`, `PER_MONTH`, `_add_months`, `advance_expected_on`, `match_cadence`. Add:

```python
def monthlyize(expected_cents: int, cadence: Cadence) -> int:
    return round(expected_cents * PER_MONTH[cadence])
```

Module imports only `statistics`, `calendar`, `datetime`, and `moneta.models.Cadence` — nothing from pipelines/queries (this module exists to break that cycle).

- [ ] **Step 2:** `recurring.py` imports them (`from moneta.cadence import CADENCE_DAYS, GRACE_DAYS, PER_MONTH, TOLERANCE, advance_expected_on, match_cadence, monthlyize`), deletes the moved definitions, and `monthly_cents` becomes:

```python
def monthly_cents(series: RecurringSeries) -> int:
    return monthlyize(series.expected_cents, series.cadence)
```

Update internal call sites (`_match_cadence` → `match_cadence`, `_TOLERANCE` → `TOLERANCE`, `_PER_MONTH` → `PER_MONTH`).

- [ ] **Step 3:** Full gate (pure move — the suite is the regression net). Commit: `refactor: extract pure cadence math to moneta.cadence (unblocks queries-side reuse)`

---

### Task 3: `ClassifiedLink` grows `outflow_amount_cents` + `inflow_is_loan_like`; `loan_payment_stats`

**Files:**
- Modify: `src/moneta/queries.py`
- Test: `tests/test_queries.py` (create if absent — check for existing coverage in other files first via `grep -rn "classified_links" tests/`)

**Interfaces:**
- Produces: `ClassifiedLink` fields `outflow_amount_cents: int`, `inflow_is_loan_like: bool` (inflow account is `type == loan` OR `financing_mode`); `LoanPayment(BaseModel)` with `account_id: int`, `cadence: Cadence`, `expected_cents: int` (negative), `last_paid_on: date`; `loan_payment_stats(links: list[ClassifiedLink]) -> dict[int, LoanPayment]`.

- [ ] **Step 1: Failing tests** — new `tests/test_queries.py::test_loan_payment_stats_groups_by_inflow_account`: build `ClassifiedLink` values directly (frozen dataclass, no DB needed) — two loan accounts (ids 10, 11), account 10 with monthly payments −6444 on the 1st × 3 months, account 11 with −10643 × 3 months offset; assert two entries, `expected_cents == -6444` / `-10643`, `cadence == Cadence.monthly`. `test_loan_payment_stats_two_payments_falls_back_monthly`: 2 payments 30 days apart → monthly, correct median. `test_loan_payment_stats_ignores_non_loan_like`: a credit-inflow link contributes nothing. Also `test_classified_links_flags_loan_like` (DB test, pattern from existing classified_links usage): three accounts — loan, financing-mode credit, plain credit — each with a linked payment pair; assert `inflow_is_loan_like` True/True/False and `outflow_amount_cents` carries the outflow's amount.

- [ ] **Step 2: Implement.** In `classified_links`: add `Transaction.amount_cents` to the txn select; fetch `(Account.id, Account.type, Account.financing_mode)` rows (replacing the bare `account_type_map` call) and compute `inflow_is_loan_like = in_type == AccountType.loan or in_financing`. In the dataclass, add both fields. Add:

```python
class LoanPayment(BaseModel):
    account_id: int
    cadence: Cadence
    expected_cents: int  # negative: outflow convention
    last_paid_on: date


def loan_payment_stats(links: list[ClassifiedLink]) -> dict[int, LoanPayment]:
    """Per-loan-account payment cadence/amount derived from transfer-linked outflows.

    Banks collapse different loans' payments into one descriptor; the link's inflow
    account is the reliable per-loan identity (design 2026-07-16 §3).
    """
    by_account: dict[int, list[ClassifiedLink]] = {}
    for link in links:
        if link.inflow_is_loan_like:
            by_account.setdefault(link.inflow_account_id, []).append(link)
    result: dict[int, LoanPayment] = {}
    for account_id, group in by_account.items():
        dates = sorted({link.outflow_posted_on for link in group})
        match = match_cadence(dates)
        if match is None:
            cadence, run_start = Cadence.monthly, dates[0]  # loans are near-universally monthly
        else:
            cadence, run_start = match
        amounts = [
            abs(link.outflow_amount_cents) for link in group if link.outflow_posted_on >= run_start
        ]
        result[account_id] = LoanPayment(
            account_id=account_id,
            cadence=cadence,
            expected_cents=-round(statistics.median(amounts)),
            last_paid_on=dates[-1],
        )
    return result
```

(imports: `statistics`, `from pydantic import BaseModel`, `from moneta.cadence import match_cadence`, `from moneta.models import Cadence` — cadence.py has no queries dependency, so no cycle.)

- [ ] **Step 3:** Gate + commit: `feat(queries): loan-like link classification and per-account payment derivation`

---

### Task 4: Financing fingerprint pipeline + `run_sync` wiring + resolution

**Files:**
- Create: `src/moneta/pipelines/financing.py`
- Modify: `src/moneta/pipelines/run.py` (SyncReport + call), `src/moneta/pipelines/review.py` (`apply_resolution`, `_REQUIRED_BOOL` in api.py — see below), `src/moneta/api.py` (`_REQUIRED_BOOL`), `src/moneta/cli/main.py` (`_REVIEW_KINDS`, `_review_one`)
- Test: `tests/test_financing_detect.py` (new)

**Interfaces:**
- Produces: `detect_financing(session) -> int` (questions opened; commits); `SyncReport.financing_questions: int`; resolution `{"financing": bool}` sets `Account.financing_mode`.

- [ ] **Step 1: Failing tests** — `tests/test_financing_detect.py`, fixtures shaped on the spec's real accounts (use `make_account`/`make_txn` from tests/factories.py; dates relative to a fixed anchor are fine — the fingerprint takes no `today`):
  - `test_fires_on_payments_only`: credit account, balance −161105, credits +6444 × 3 monthly, zero debits → 1 open `financing_account` item, payload `{"account_id": id}`.
  - `test_payoff_outlier_still_fires` (CareCredit): credits +9900, +9600, +284794; one −299 debit (paper fee, < 25% of median 9900) → fires.
  - `test_purchase_before_payments_fires` (Modani): debit −349324 on day 0, credits +300000 on day 7 and +300000 day 37 → fires (significant debit predates earliest credit).
  - `test_daily_use_never_fires` (OnePay): 10 debits −2000..−8000 interleaved after credits began, 3 credits → nothing.
  - `test_positive_balance_never_fires`, `test_one_payment_never_fires`.
  - `test_asked_once_never_reasked`: resolved-false item exists → second run opens nothing.
  - `test_resolution_true_sets_financing_mode`: resolve the item `{"financing": true}` via `apply_resolution` → `account.financing_mode is True`; resolve false → stays False.

- [ ] **Step 2: Implement `pipelines/financing.py`:**

```python
"""Detect credit accounts used as promo-financing vehicles (design 2026-07-16 §2).

Fingerprint: owed balance, periodic near-equal payment credits, and no significant
purchase debits since payments began. Deterministic — fires a one-time human review
question; the ReviewItem ledger (open or resolved) is the never-re-ask memory.
"""

import statistics

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, ReviewItem, ReviewKind, Transaction

_AMOUNT_TOLERANCE = 0.20  # near-equal payments: within ±20% of the credit median
_MINOR_FRACTION = 0.25  # debits under 25% of the median credit are fees, not purchases
_MIN_PAYMENTS = 2


async def detect_financing(session: AsyncSession) -> int:
    asked: set[int] = set()
    items = (
        await session.execute(
            select(ReviewItem).where(ReviewItem.kind == ReviewKind.financing_account)
        )
    ).scalars()
    for item in items:
        account_id = item.payload.get("account_id")
        if isinstance(account_id, int):
            asked.add(account_id)
    candidates = (
        (
            await session.execute(
                select(Account).where(
                    Account.type == AccountType.credit,
                    Account.financing_mode.is_(False),
                    Account.balance_cents < 0,
                )
            )
        )
        .scalars()
        .all()
    )
    opened = 0
    for acct in candidates:
        if acct.id in asked:
            continue
        txns = (
            (
                await session.execute(
                    select(Transaction).where(Transaction.account_id == acct.id)
                )
            )
            .scalars()
            .all()
        )
        credits = sorted((t for t in txns if t.amount_cents > 0), key=lambda t: t.posted_on)
        if len(credits) < _MIN_PAYMENTS:
            continue
        median_credit = statistics.median([t.amount_cents for t in credits])
        near_median = sum(
            1
            for t in credits
            if abs(t.amount_cents - median_credit) <= median_credit * _AMOUNT_TOLERANCE
        )
        if near_median < 2:
            continue
        payments_began = credits[0].posted_on
        purchasing = any(
            t.amount_cents < 0
            and abs(t.amount_cents) >= median_credit * _MINOR_FRACTION
            and t.posted_on >= payments_began
            for t in txns
        )
        if purchasing:
            continue
        session.add(
            ReviewItem(
                kind=ReviewKind.financing_account,
                question=(
                    f"{acct.name!r} looks like promo financing being paid down — "
                    "treat its payments as fixed costs?"
                ),
                payload={"account_id": acct.id},
            )
        )
        opened += 1
    await session.commit()
    return opened
```

- [ ] **Step 3: Wire up.** `run.py`: `SyncReport` gains `financing_questions: int`; in `run_sync`, after `link_transfers` and before `autoreview_items`: `financing_questions = await detect_financing(session)`; pass into the report. `review.py::apply_resolution` gains the branch (before the final two lines):

```python
    elif item.kind == ReviewKind.financing_account and isinstance(
        resolution.get("financing"), bool
    ):
        acct = await session.get(Account, item.payload["account_id"])
        if acct is not None:
            acct.financing_mode = resolution["financing"]
```

`api.py` `_REQUIRED_BOOL` gains `ReviewKind.financing_account: "financing"`. `cli/main.py`: `_REVIEW_KINDS` gains `"financing_account": ("financing check", "confirming counts this card's payments as fixed costs in moneta power")`; `_review_one` gains, before the generic fallback:

```python
    if item["kind"] == "financing_account":
        answer = _prompt_yes_no("Treat as financing? [y/n]")
        return None if answer is None else {"financing": answer}
```

`autoreview_items` needs no change (unhandled kinds fall to `continue`) — add `test_autoreview_skips_financing_account` asserting an open financing item survives an autoreview run with a scripted LLM untouched. Existing `run_sync` tests: update for the new `SyncReport` field (check `grep -rn "SyncReport(" tests/ src/`).

- [ ] **Step 4:** Gate + commit: `feat(pipelines): financing-mode fingerprint with one-time review question`

---

### Task 5: Power spend buckets — discretionary + ended series

**Files:**
- Modify: `src/moneta/views/power.py`
- Test: `tests/test_power.py`

**Interfaces:**
- Produces: `power_report` where `income_sources`/`fixed_costs` include only `discretionary == False` series, and `spent_so_far_cents` counts a txn unless it is tagged to an **active, non-discretionary** series (linked txns stay excluded as today).

- [ ] **Step 1: Failing tests** (test_power.py): `test_ended_series_txns_count_as_spent` — active series ended (`status=SeriesStatus.ended`), a this-month txn tagged to it → included in `spent_so_far_cents`, `remaining_cents` reflects it (the ended-series ticket's acceptance test). `test_discretionary_series_excluded_from_fixed_and_counted_as_spend` — `make_series(discretionary=True, expected_cents=-3886)` + tagged this-month txn −3886 → `total_fixed_cents` unchanged by it, txn in `spent_so_far_cents`. `test_discretionary_inflow_not_income` — inflow series with `discretionary=True` → not in `income_sources`.

- [ ] **Step 2: Implement.** Series filter (line ~72): both `_series_lines` calls filter `and not s.discretionary`. Spend query: replace the `Transaction.series_id.is_(None)` condition with an outer join:

```python
        select(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .outerjoin(RecurringSeries, Transaction.series_id == RecurringSeries.id)
        .where(
            Transaction.amount_cents < 0,
            Transaction.posted_on >= month_start,
            Transaction.posted_on <= today,
            or_(
                Transaction.series_id.is_(None),
                RecurringSeries.status != SeriesStatus.active,
                RecurringSeries.discretionary.is_(True),
            ),
            Account.type.in_(SPEND_ACCOUNT_TYPES),
            Account.currency == primary,
        )
```

(`from sqlalchemy import or_, select`.)

- [ ] **Step 3:** Gate + commit: `feat(power): ended and discretionary series txns count as spend; discretionary never a fixed cost`

---

### Task 6: Obligations from link derivation (+ loan-like inclusion)

**Files:**
- Modify: `src/moneta/views/financing.py`
- Test: `tests/test_financing.py`

**Interfaces:**
- Consumes: `loan_payment_stats`, `monthlyize`, `ClassifiedLink.inflow_is_loan_like`.
- Produces: `compute_obligations` covering accounts where `type == loan` OR `financing_mode`, with `monthly_payment_cents = abs(monthlyize(lp.expected_cents, lp.cadence))` from the derivation; `_payment_series_id` deleted.

- [ ] **Step 1: Failing tests** (test_financing.py): rewrite the payment fixtures from series+link to plain link pairs (outflow txn from checking + inflow txn on the loan + `TransferLink`) — `test_merged_descriptors_two_loans_two_obligations` (the ticket's acceptance case): two loan accounts, payments with IDENTICAL descriptions but different amounts (−6444 vs −10643) and dates, 3 each → two obligations with per-account `monthly_payment_cents` 6444 / 10643. `test_two_payments_monthly_fallback`: loan with 2 linked payments → payment present (not None). `test_financing_mode_account_gets_obligation`: financing-mode credit account with linked payments + `promo_expires_on` set near → obligation row present with `deferred_interest_risk` computed. Keep/adapt the no-payment `?` test (no links → `monthly_payment_cents is None`).

- [ ] **Step 2: Implement.** Loans query becomes accounts where `(Account.type == AccountType.loan) | (Account.financing_mode.is_(True))`, `balance_cents != 0`. Delete `_payment_series_id` and the `RecurringSeries` lookup; per loan:

```python
        lp = payments.get(loan.id)  # payments = loan_payment_stats(links), computed once
        payment_cents = abs(monthlyize(lp.expected_cents, lp.cadence)) if lp else None
```

months_left/payoff/risk logic unchanged.

- [ ] **Step 3:** Gate + commit: `feat(obligations): per-account payments derived from transfer links`

---

### Task 7: Power loan-payment lines + cc-filter correction

**Files:**
- Modify: `src/moneta/views/power.py`
- Test: `tests/test_power.py`

**Interfaces:**
- Consumes: `loan_payment_stats`, `monthlyize`, `inflow_is_loan_like`.
- Produces: fixed-cost lines `SeriesLine(merchant=f"{account.name} — payment", cadence=lp.cadence, monthly_cents=abs(monthlyize(...)))` per loan-like account; `cc_series` excludes only links into credit accounts that are NOT loan-like.

- [ ] **Step 1: Failing tests**: `test_loan_payment_lines_in_fixed_costs` — two loan accounts with linked payments (no series) → two fixed-cost lines labeled `"<name> — payment"`, `total_fixed_cents` includes both; `test_financing_mode_payments_are_fixed_costs_not_cc_excluded` — financing-mode credit account with a linked payment series → its payment appears (not filtered as a cc payment).

- [ ] **Step 2: Implement.** `cc_series` condition: `link.inflow_account_type == AccountType.credit and not link.inflow_is_loan_like`. After `fixed, total_fixed = _series_lines(...)`, append derived lines:

```python
    payments = loan_payment_stats(links)
    if payments:
        names = dict(
            (
                await session.execute(
                    select(Account.id, Account.name).where(Account.id.in_(payments))
                )
            ).all()
        )
        for lp in payments.values():
            line = SeriesLine(
                merchant=f"{names.get(lp.account_id, f'account {lp.account_id}')} — payment",
                cadence=lp.cadence,
                monthly_cents=abs(monthlyize(lp.expected_cents, lp.cadence)),
            )
            fixed.append(line)
            total_fixed += line.monthly_cents
        fixed.sort(key=lambda line: line.monthly_cents, reverse=True)
```

- [ ] **Step 3:** Gate + commit: `feat(power): per-loan-account payment lines; financing payments are fixed costs`

---

### Task 8: Detection exclusion — loan payments leave merchant grouping

**Files:**
- Modify: `src/moneta/pipelines/recurring.py` (`_excluded_txn_ids`, `detect_recurring` untag pass + sweep)
- Test: `tests/test_recurring.py`

**Interfaces:**
- Produces: `_excluded_txn_ids(session) -> tuple[set[int], set[int]]` — `(excluded, loan_payment_outflows)`; ALL linked txns are now excluded from grouping; loan-like payment outflows additionally get untagged, and an active series left with zero tagged txns is ended.

- [ ] **Step 1: Failing tests**: `test_loan_linked_payments_form_no_series` — 3 monthly outflows same merchant, each linked to a loan-account inflow → `detect_recurring` creates no series; `test_existing_merged_payment_series_ends` — pre-seeded series tagged on loan-linked txns → after one run, txns untagged, series `status == SeriesStatus.ended`; `test_partial_untag_keeps_series` — series with both loan-linked and genuine txns keeps its genuine occurrences and stays active.

- [ ] **Step 2: Implement.**

```python
async def _excluded_txn_ids(session: AsyncSession) -> tuple[set[int], set[int]]:
    """All transfer-linked txns are excluded from merchant grouping. Loan-like payment
    outflows are additionally untagged — their per-account derivation lives in
    queries.loan_payment_stats, not in a merchant series (design 2026-07-16 §3)."""
    excluded: set[int] = set()
    loan_payment_outflows: set[int] = set()
    for link in await classified_links(session):
        excluded.add(link.inflow_id)
        excluded.add(link.outflow_id)
        if link.inflow_is_loan_like:
            loan_payment_outflows.add(link.outflow_id)
    return excluded, loan_payment_outflows
```

In `detect_recurring`, unpack both; in the existing merchant-correction untag loop (or directly after it) add:

```python
    untagged_series: set[int] = set()
    for t in txns:
        if t.id in loan_payment_outflows and t.series_id is not None:
            untagged_series.add(t.series_id)
            t.series_id = None
```

In the final sweep, end orphaned series: where the current code does `if last_seen is not None and _stale(...)`, add the orphan case first:

```python
        last_seen = newest_by_series.get(series.id)
        if last_seen is None:
            if series.id in untagged_series:  # its txns were loan payments — derivation owns them now
                series.status = SeriesStatus.ended
                stats.updated += 1
            continue
        if _stale(last_seen, series.cadence, today):
```

- [ ] **Step 3:** Gate + commit: `feat(recurring): loan-linked payments leave merchant grouping; orphaned merged series end`

---

### Task 9: Three-way classification in detection

**Files:**
- Modify: `src/moneta/pipelines/recurring.py` (`_LLM_PROMPT`, force map, unstable branch, series create/update)
- Test: `tests/test_recurring.py`

**Interfaces:**
- Produces: `_LLM_PROMPT` asking `{"classification": "bill"|"habit"|"not_recurring"}` with amount min/median/max in context; force map `force: dict[key, tuple[bool, bool]]` — `(is_recurring, discretionary)`; series `discretionary` set from LLM habit answers and force-map entries on create AND update.

- [ ] **Step 1: Failing tests**: `test_unstable_llm_habit_becomes_discretionary` (the bills-vs-habitual ticket's acceptance): weekly cadence, amounts 2176/12035/3886/4100 (spread > 2×), scripted LLM `{"classification": "habit"}` → series exists with `discretionary is True`; `test_unstable_llm_bill_stays_fixed`: same shape, `{"classification": "bill"}` → `discretionary is False`; `test_unstable_llm_not_recurring_opens_review`: `{"classification": "not_recurring"}` → no series, one review item; `test_unstable_no_llm_opens_review` (unchanged behavior, pin it); `test_stable_subscription_unchanged`: stable monthly, no LLM → detected, `discretionary is False`; `test_force_map_habit_applies_on_update`: resolved ledger item `{"is_recurring": true, "discretionary": true}` → existing active series gains `discretionary=True` on the next run. Update any existing test asserting the old `is_recurring` LLM answer shape (grep `is_recurring` in tests/test_recurring.py — the ScriptedLLM answers change to the new schema).

- [ ] **Step 2: Implement.** New prompt:

```python
_LLM_PROMPT = """Classify this group of bank transactions from one merchant.
- "bill": a fixed obligation — subscription, rent, insurance, loan or membership payment; \
roughly stable amount; there are consequences if unpaid.
- "habit": recurring discretionary spending — restaurants, coffee, bars, groceries, \
rideshare; variable amounts; a fresh choice each time.
- "not_recurring": neither — coincidental repetition.
Merchant: {merchant!r}; amount cents min/median/max: {lo}/{med}/{hi}
Amounts (cents) and dates: {rows}
Respond with JSON: {{"classification": "bill" | "habit" | "not_recurring"}}"""
```

Force map parsing (ledger loop): `force[key] = (item.resolution["is_recurring"], bool(item.resolution.get("discretionary")))`. Group loop: `forced = force.get((merchant, str(direction)))`; `if forced is not None and forced[0] is False: continue`; `discretionary = forced[1] if forced is not None else False`. Unstable branch becomes:

```python
        elif not stable and (forced is None or forced[0] is not True):
            answer = (
                await llm.classify_json(
                    _LLM_PROMPT.format(
                        merchant=merchant,
                        lo=min(amounts),
                        med=round(med),
                        hi=max(amounts),
                        rows=[(t.amount_cents, t.posted_on.isoformat()) for t in run],
                    )
                )
                if llm
                else None
            )
            classification = answer.get("classification") if answer else None
            if classification == "habit":
                discretionary = True
            elif classification != "bill":
                _open_review()
                continue
```

(The cadence-`None` forced check becomes `forced is None or forced[0] is not True` correspondingly.) Series create: `discretionary=discretionary` in the constructor. Series update path: `series.discretionary = discretionary` alongside cadence/status (include in the `changed` computation).

- [ ] **Step 3:** Gate + commit: `feat(recurring): three-way bill/habit/not-recurring classification with amount spread`

---

### Task 10: Three-way verify + human review flow

**Files:**
- Modify: `src/moneta/pipelines/review.py` (`_VERIFY_PROMPT`, `verify_series`, `apply_resolution` recurring_cluster-True branch, `_validated` recurring branch)
- Modify: `src/moneta/cli/main.py` (`_review_one` recurring_cluster prompt)
- Test: `tests/test_autoreview.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `_VERIFY_PROMPT` with the same three-way definitions + amount spread, response `{"classification": ..., "confident": bool}`; confident `bill` → resolved `{"is_recurring": true}` (as today); everything else → open item with `payload["llm_flagged"] = true` and `payload["llm_leaning"] = <classification or "unparseable">`. `apply_resolution` with `is_recurring: true` sets/clears `discretionary` on matching series (any status). CLI recurring_cluster prompt: `b`/`h`/`n` (`y`→bill, `no`→n accepted).

- [ ] **Step 1: Failing tests** (test_autoreview.py): `test_verify_confident_bill_resolves` (adapt the existing confident-yes test to the new answer shape `{"classification": "bill", "confident": True}`); `test_verify_confident_habit_flags_with_leaning`: `{"classification": "habit", "confident": True}` → open item, `llm_flagged` True, `llm_leaning == "habit"`, series still active and non-discretionary; existing unconfident/confident-no tests adapted to the new schema. `test_apply_resolution_habit_sets_discretionary`: resolved `{"is_recurring": True, "discretionary": True}` on an item whose merchant has an active series → `series.discretionary is True`. test_cli.py: `test_review_recurring_three_way` — scripted input `h` resolves `{"is_recurring": True, "discretionary": True}` (assert via the posted resolution with a fake `request`).

- [ ] **Step 2: Implement.** `_VERIFY_PROMPT` mirrors Task 9's definitions (keep `Set confident=true ONLY if you are sure either way.`), response `{{"classification": "bill" | "habit" | "not_recurring", "confident": true/false}}`; include `expected` and min/median/max of the sample amounts. `verify_series` answer handling: confident+bill → `apply_resolution(..., {"is_recurring": True}, resolved_by="llm")` (unchanged call); else open item with `llm_leaning` in the payload. `_validated`'s recurring branch: accept `discretionary` bool passthrough when present. `apply_resolution`: after the `is_recurring is False` branch add:

```python
    elif item.kind == ReviewKind.recurring_cluster and resolution.get("is_recurring") is True:
        stmt = select(RecurringSeries).where(
            RecurringSeries.merchant == item.payload.get("merchant")
        )
        direction = item.payload.get("direction")
        if direction is not None:
            stmt = stmt.where(RecurringSeries.direction == direction)
        for series in (await session.execute(stmt)).scalars():
            series.discretionary = bool(resolution.get("discretionary"))
```

CLI `_review_one` recurring_cluster branch:

```python
    if item["kind"] == "recurring_cluster":
        for s in ctx.get("samples", []):
            console.print(f"    {s['posted_on']}  {fmt_money(abs(s['amount_cents']))}")
        if ctx.get("direction") == "inflow":
            console.print("    [dim](these are deposits — answering b counts them as income)[/dim]")
        answer = typer.prompt(
            "Bill, habit, or not recurring? [b/h/n]", default="", show_default=False
        )
        normalized = answer.strip().lower()
        if not normalized:
            return None
        if normalized in ("b", "bill", "y", "yes"):
            return {"is_recurring": True}
        if normalized in ("h", "habit"):
            return {"is_recurring": True, "discretionary": True}
        if normalized in ("n", "no", "not"):
            return {"is_recurring": False}
        console.print("[red]invalid input, skipping[/red]")
        return None
```

Update `_REVIEW_KINDS["recurring_cluster"]` copy: `("bill or habit question", "bills become fixed costs; habits stay discretionary spending in moneta power")`. Update the user-guide review table row in Task 14, not here.

- [ ] **Step 3:** Gate + commit: `feat(review): three-way verify and b/h/n human review`

---

### Task 11: Cadence fallback tolerance

**Files:**
- Modify: `src/moneta/pipelines/recurring.py` (`_closest_cadence`, its call site) — note `_median_gap` may lose its last caller; delete it if so
- Test: `tests/test_recurring.py`

**Interfaces:**
- Produces: `_closest_cadence(dates) -> Cadence` — nearest cadence to the NEWEST gap, only within that cadence's `TOLERANCE`; monthly otherwise and when fewer than 2 unique dates.

- [ ] **Step 1: Failing tests**: `test_forced_cadence_prefers_newest_gap` (the Lifetime Fitness case): charges day 0/+12/+42, forced-recurring ledger entry, no LLM → series cadence `monthly` (old code: biweekly); `test_forced_single_date_monthly_no_crash`: 3 same-day txns forced → monthly series, no exception; existing matched-run tests unchanged.

- [ ] **Step 2: Implement:**

```python
def _closest_cadence(dates: list[date]) -> Cadence:
    """Forced-in groups have no clean run; the newest gap is the least-stale evidence.
    A between-buckets median once billed a $265.72 gym at biweekly ($575/mo)."""
    if len(dates) < 2:
        return Cadence.monthly
    newest_gap = (dates[-1] - dates[-2]).days
    candidate = min(CADENCE_DAYS, key=lambda c: abs(CADENCE_DAYS[c] - newest_gap))
    if abs(CADENCE_DAYS[candidate] - newest_gap) <= TOLERANCE[candidate]:
        return candidate
    return Cadence.monthly  # safest: factor 1.0 can't inflate the monthly number
```

Also guard the cadence-miss review gate (line ~251) for `len(dates) < 2` (skip `_median_gap`, don't open — a single date is never bill-like timing). If `_median_gap` now has no callers, delete it.

- [ ] **Step 3:** Gate + commit: `fix(recurring): forced-cadence fallback uses newest gap with tolerance; monthly otherwise`

---

### Task 12: Overrule endpoints + CLI (`--not-a-bill` / `--habit` / `--re-review`)

**Files:**
- Modify: `src/moneta/api.py` (three POST endpoints), `src/moneta/cli/main.py` (`recurring` command flags)
- Test: `tests/test_api.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `POST /recurring/{id}/not-a-bill`, `POST /recurring/{id}/habit`, `POST /recurring/{id}/re-review` (404 on unknown id, `{"ok": true}` otherwise); CLI flags, mutually exclusive with each other and `--end`.

- [ ] **Step 1: Failing tests** (test_api.py): `test_not_a_bill_ends_series_flips_ledger_and_suppresses` (the ticket's acceptance): seed txns forming a series + a resolved-True llm ledger item; POST not-a-bill → series ended, item resolution `{"is_recurring": False, "resolved_by": "manual"}`; run `detect_recurring` → no new series, still ended. `test_habit_marks_discretionary_and_reactivates_if_ended`; `test_re_review_reopens_item`; `test_overrule_unknown_series_404`. (test_cli.py): `test_recurring_not_a_bill_flag_posts` (fake `request`, assert the POST path), `test_recurring_overrule_flags_mutually_exclusive` (two flags → exit 1, clean message).

- [ ] **Step 2: Implement.** In api.py, one shared helper inside `create_app`:

```python
    async def _series_ledger_item(session: AsyncSession, series_id: int) -> tuple[
        RecurringSeries, ReviewItem
    ]:
        series = (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.id == series_id)
            )
        ).scalar_one_or_none()
        if series is None:
            raise HTTPException(status_code=404, detail="series not found")
        item = (
            await session.execute(
                select(ReviewItem).where(
                    ReviewItem.kind == ReviewKind.recurring_cluster,
                    ReviewItem.payload["merchant"].as_string() == series.merchant,
                    ReviewItem.payload["direction"].as_string() == series.direction,
                )
            )
        ).scalars().first()
        if item is None:
            item = ReviewItem(
                kind=ReviewKind.recurring_cluster,
                question=f"Is {series.merchant!r} a recurring bill?",
                payload={"merchant": series.merchant, "direction": series.direction},
            )
            session.add(item)
            await session.flush()
        return series, item
```

(If `payload[...].as_string()` misbehaves on SQLite JSON, fall back to fetching all recurring_cluster items and matching via `series_key(item.payload.get("merchant"), item.payload.get("direction")) == series_key(series.merchant, series.direction)` in Python — note which you used in the report.) Endpoints:

```python
    @app.post("/recurring/{series_id}/not-a-bill")
    async def not_a_bill(series_id: int, session: Session) -> dict[str, bool]:
        _series, item = await _series_ledger_item(session, series_id)
        item.status = ReviewStatus.open  # reopen a resolved item so apply_resolution re-runs
        await apply_resolution(session, item, {"is_recurring": False}, resolved_by="manual")
        await session.commit()
        return {"ok": True}

    @app.post("/recurring/{series_id}/habit")
    async def mark_habit(series_id: int, session: Session) -> dict[str, bool]:
        series, item = await _series_ledger_item(session, series_id)
        item.status = ReviewStatus.open
        await apply_resolution(
            session, item, {"is_recurring": True, "discretionary": True}, resolved_by="manual"
        )
        if series.status != SeriesStatus.active:
            reactivate_series(series, today=date.today())
        await session.commit()
        return {"ok": True}

    @app.post("/recurring/{series_id}/re-review")
    async def re_review(series_id: int, session: Session) -> dict[str, bool]:
        _series, item = await _series_ledger_item(session, series_id)
        item.status = ReviewStatus.open
        item.resolution = None
        await session.commit()
        return {"ok": True}
```

CLI `recurring` command gains `not_a_bill: int | None = typer.Option(None, "--not-a-bill")`, `habit: int | None`, `re_review: int | None`; count the mutually-exclusive group `(end, not_a_bill, habit, re_review)` — more than one set → red error + `raise typer.Exit(1)`; each flag POSTs its endpoint and prints a one-line confirmation (e.g. `Series 4 marked not-a-bill — suppressed from future detection.`). Update the command help string.

- [ ] **Step 3:** Gate + commit: `feat(api,cli): --not-a-bill / --habit / --re-review overrule doors`

---

### Task 13: `--set-financing` + accounts display

**Files:**
- Modify: `src/moneta/api.py` (`AccountPatch`), `src/moneta/cli/main.py` (`accounts`)
- Test: `tests/test_api.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `AccountPatch.financing_mode: bool | None`; PATCH applies it; CLI `moneta accounts --set-financing ID true|false`; accounts table shows `credit (financing)` for financing-mode accounts.

- [ ] **Step 1: Failing tests**: `test_patch_financing_mode` (API): PATCH `{"financing_mode": true}` → account flag set; false clears. `test_accounts_set_financing_flag` (CLI, fake request): `--set-financing 3 true` → PATCH body `{"financing_mode": True}`; `test_accounts_shows_financing_marker`: seeded financing account renders `credit (financing)`.

- [ ] **Step 2: Implement.** `AccountPatch` gains `financing_mode: bool | None = None`; patch endpoint applies when in `model_fields_set`. CLI: `set_financing: tuple[int, str] | None = typer.Option(None, "--set-financing")`; parse `true|false` (anything else → red error, exit 1); table Type cell: `f"{a['type']} (financing)" if a.get("financing_mode") else a["type"]` — which requires `AccountOut` to expose `financing_mode: bool`; add it.

- [ ] **Step 3:** Gate + commit: `feat(accounts): financing-mode patch, CLI flag, and display marker`

---

### Task 14: `_STORE_NUM` hyphen fix

**Files:**
- Modify: `src/moneta/pipelines/normalize.py`
- Test: `tests/test_normalize.py` (or wherever `rule_normalize` tests live — `grep -rn "rule_normalize" tests/`)

- [ ] **Step 1: Failing tests**: `rule_normalize("1-800-FLOWERS.COM") == "1-800-Flowers.Com"` (digit run kept), `rule_normalize("BLUE BOTTLE #1234") == "Blue Bottle"`, `rule_normalize("STORE 4521") == "Store"` (unchanged behaviors pinned).

- [ ] **Step 2: Implement:** `_STORE_NUM = re.compile(r"(#\d+|(?<![\w-])\d{3,}(?![\w-]))")`.

- [ ] **Step 3:** Gate + commit: `fix(normalize): keep hyphen-joined digit runs (1-800-FLOWERS)`

---

### Task 15: Docs + ticket cleanup

**Files:**
- Modify: `docs/PRD.md`, `docs/user-guide.md`, `CLAUDE.md`
- Delete (git rm): the 7 ticket files listed in the spec header

- [ ] **Step 1:** PRD: feature-history entries (three-way classification + discretionary; financing fingerprint; link-derived loan payments; overrule CLI) dated to the merge; remove the shipped items from roadmap/backlog mentions (grep each ticket's title).
- [ ] **Step 2:** user-guide: review-queue table row for recurring becomes `b`/`h`/`n` with one-line definitions; new financing-check row; `recurring` section documents `--not-a-bill/--habit/--re-review`; `accounts` section documents `--set-financing` and the `credit (financing)` marker; obligations section notes payments derive per-account from transfers (merged descriptors handled); a "Financing cards" subsection with the hybrid-card limitation.
- [ ] **Step 3:** CLAUDE.md conventions: update the transfer-link bullet (all linked txns excluded from detection; loan-like payments derived per-account at view time via `queries.loan_payment_stats`), series lifecycle (discretionary semantics), pipeline order (financing-detect stage), LLM ledger (three-way, `llm_leaning`), and the layout line for `cadence.py` + `pipelines/financing.py`.
- [ ] **Step 4:** `git rm` the seven tickets; full gate; commit: `chore: docs + close wave-2 backlog tickets`

---

### Task 16: Review gates + PR

- [ ] **Step 1:** Architecture review subagent over the whole branch; fix every Critical/Important.
- [ ] **Step 2:** /simplify pass; apply findings.
- [ ] **Step 3:** Final whole-branch review (most capable model); fix or triage findings.
- [ ] **Step 4:** QA-backlog subagent (candidates: real-data financing fingerprint behavior, review flow b/h/n ergonomics).
- [ ] **Step 5:** Verify skill smoke test (seeded DB: financing account fires the question; loan payments appear per-account in power/obligations; b/h/n review).
- [ ] **Step 6:** Push, `gh pr create` — summary references the spec, lists the 7 closed tickets, and the standard footer.
