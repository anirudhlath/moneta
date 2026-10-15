# Transfer Scoring and History Income Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transfer links are made by exact, auditable rules that stale links can't outlive,
and `power --history` income means the same thing as `power` income.

**Architecture:**
- Transfer pairs are scored in integer tenths by a public `score_pair` → `PairScore`.
  A rule link needs the threshold **and** a transfer keyword.
- Payment-app rail wording is stripped before the keyword search, and `ach` matches
  only as a whole word.
- `link_transfers` starts by re-scoring every existing `rule` link with the same
  `score_pair` and dropping the ones that no longer qualify. The freed legs are matched
  again in the same run.
- `accrual_by_month` splits inflows into income (tagged to a non-discretionary inflow
  series) and other inflows. `GET /power/history` and the CLI table show both.

**Tech Stack:** Python 3.13, FastAPI, typer + rich, SQLAlchemy async + SQLite, pytest
(asyncio auto mode), uv.

**Spec:** GitHub issues #46 and #19, plus the Background section of this plan. There
is no spec document.

## Global Constraints

- Money is integer cents everywhere. Negative = outflow in `Transaction.amount_cents`.
  Every `PowerMonth` field except `net_cents` is an unsigned magnitude.
- Pair scores are **integer tenths**: base 5; +2 when the legs post 0–1 days apart,
  else +1 at 2–3 days; +2 for a transfer keyword in either description; +1 when the
  **outflow** leaves a checking or savings account. `_AUTO_LINK = 8`. `_MAX_DAYS = 6`
  is unchanged.
- `TransferLink.confidence` stays a `Float` column (no migration, no `_HEAD` bump). It
  stores tenths ÷ 10. Links chosen in review keep 1.0 (manual) and 0.9 (LLM).
- A `rule` link needs a keyword match, whatever the score. A lone candidate below the
  rule keeps today's path: an LLM pick when an LLM is configured, else a `transfer_pair`
  review item.
- P2P rails are a module constant `_P2P_RAILS`, case-insensitive, covering
  `zelle payment (from|to)`, `venmo payment` and `paypal inst xfer`. There is no
  owner-name config.
- Transfer keywords are
  `payment|pymt|transfer|xfer|\bach\b|autopay|epay|billpay|deposit`, case-insensitive.
  Only `ach` is a whole word. Every other keyword stays a substring match, so glued
  descriptors such as `ONETIMEPAYMENT` still count, and so does `deposit` inside
  `DEPOSITORY`.
- The re-score touches only `method == "rule"` links. It never touches `manual` or
  `llm` links.
- History income = inflows tagged to a non-discretionary inflow series, the same
  definition as `views/power.py` income. Every other counted inflow is
  `other_inflows_cents`. `net_cents = income_cents - spend_cents`.
- LLM output is classification only; it never supplies a money value.
- Pipelines commit; views don't. Enum columns load as plain `str`: compare with `==`.
- The repo is **public**. Use only fictitious examples (Acme, Corner Curry, Jane Doe)
  and round amounts in code, tests, docs and commit messages.
- Commits use `git commit -s`, with no AI co-author trailers.
- Gate, required before every commit:
  `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  All four must pass with pristine output; a warning is a failure. During a task's
  red/green steps, run only the narrow test file.
- Docs upkeep happens inside the task that changes the behavior: `docs/PRD.md`,
  `docs/user-guide.md`, `README.md`, and the `CLAUDE.md` paragraph the change
  contradicts. The exact text is given in each task.
- Work in this branch's worktree (branch `fix-transfer-scoring`).

## Review Focus

1. **A refund on a credit card** (positive amount on a `credit` account) is not
   income. It shows under other inflows, and spend is not netted. Pinned in Task 4
   (`test_card_refund_is_another_inflow_not_income`).
2. **A paycheck series that ended mid-history** still counts as income in the months
   it paid. Pinned in Task 4
   (`test_ended_paycheck_series_counts_in_the_months_it_paid`).
3. **Same-amount, multi-candidate ties after the integer change**: equal scores tie
   exactly, the lower outflow id wins, and the loser is asked about. Reconcile keeps
   the older of two equal links. Pinned in Task 1
   (`test_equal_scores_break_ties_by_lower_outflow_id`,
   `test_reconcile_keeps_the_older_of_two_equal_links`).
4. **A rail prefix in lowercase or with extra spaces** is still stripped, and words
   after the rail still count. Pinned in Task 2
   (`test_p2p_rail_wording_is_not_a_transfer_keyword` with
   `"zelle  payment   from jane doe"`, and `test_text_after_a_rail_still_counts`).
5. **Re-scoring a link whose leg has a `duplicate_of_id` observation** judges the
   surviving occurrence, the transaction the linker scores. Pinned in Task 3
   (`test_rescore_judges_a_duplicate_observation_as_its_survivor`).

## Background

Line numbers are for `origin/main` at the time of writing.

### 1. Float scoring misses the threshold (#46)

`_confidence` (`src/moneta/pipelines/transfers.py:70-81`) sums float tenths. The
linker compares that sum with `_AUTO_LINK = 0.8` (`:50`) at `:236`. The sum
`0.5 + 0.2 + 0.1` is `0.7999999999999999`. A card payment that posts 4 days after the
checking debit therefore never auto-links: 5 + 2 (keyword) + 1 (checking outflow) is
exactly the threshold. Instead it lands in review, or worse, it is counted as spend
and income.

The same noise orders the greedy pass (`:215-219`, `max(c.confidence)`). It also
orders `_reconcile_links` (`src/moneta/pipelines/reconcile.py:59`, `-row.confidence`).
So two nominally equal scores are ranked by accident.

The score table in the issue, checked against the code:
- The +0.1 checks **only the outflow's** account type (`:79`), not any account
  involved.
- The date bonuses are `if`/`elif` (`:73-76`). They do not stack.
- Among keyword pairs, only "4–6 days + keyword + checking/savings outflow" misses
  0.8. "2–3 days + keyword + other outflow" sums to exactly `0.8` and links.

**Decision:**
- Score in integer tenths (`_AUTO_LINK = 8`). Store and expose confidence as
  tenths ÷ 10.
- Fix both sorts. Equal scores are exact ties. The lower outflow id breaks a tie,
  which is the old stable order made explicit. Reconcile compares stored
  confidences rounded to tenths.

### 2. A rule link needs no keyword today

`:236` checks only the score. "≤1 day apart + checking outflow" with no keyword is
nominally 0.8. It stayed unlinked only because of the float noise above, so fixing
defect 1 alone would start linking such pairs.

**Decision:** a rule link requires a keyword match, whatever the score. Below the
rule, behaviour is unchanged: an LLM pick if one is configured, else a review
question. This lands in Task 1, with the integer change.

### 3. P2P rail wording counts as a transfer keyword

`_TRANSFER_PAT` (`:46-48`) is an unanchored substring search. "Zelle payment from
JANE DOE" matches on `payment`. A friend's inflow and an unrelated same-day,
same-amount card outflow ("Acme Wallet: Add Money") score 5 + 2 + 2 = 9 and
rule-link. The friend's money then disappears from income, and the purchase
disappears from spend.

**Decision:** strip the rail phrases before the keyword search, using a module
constant. There is no owner-name config.

The same unanchored search finds `ach` inside ordinary words: "BEACH", "COACH", and
the "each" in brokerage text such as "for $10.00 each". Any of these can supply the +2
keyword points, and now the keyword rule, by accident.

**Decision:** match `ach` as a whole word (`\bach\b`), so "ACH PMT" and "ACH Deposit"
still match. Every other keyword stays a substring match. Word boundaries on all of
them would drop real card and loan payments whose descriptors glue words together
("ONETIMEPAYMENT", "PAYMENTS"), along with the rule links those payments have today.
`deposit` matching inside "DEPOSITORY" is accepted. Both fixes land in Task 2.

### 4. Wrong rule links never go away

`link_transfers` only adds links. Every linked transaction is excluded from
candidacy (`:133-137`). `_reconcile_links` (`reconcile.py:50-90`) re-validates
amount, date window, distinct accounts, currency and review choices, but never the
score. Ingest drops a link only when the upstream amount or date changes
(`ingest.py:148`). So a rule link made under older scoring, such as the P2P/wallet
pair above, survives every later sync.

**Decision:**
- Add a re-scoring pass that drops `rule` links that no longer qualify. Never touch
  links of any other method.
- Freed legs may re-pair in the same run.

**Placement:**
- The pass runs at the start of `link_transfers`, so it runs on every sync
  (`run.py:123`) and in `moneta reconcile` (`reconcile.py:253`).
- That is after provider-overlap reconciliation has repointed link endpoints to the
  survivors.
- It is before financing, auto-review, `detect_recurring` and `verify_series`.
  `detect_recurring` excludes linked legs from merchant groups, so a freed leg can
  join its merchant's series in the same sync.
- The views read links at request time, so `spent_so_far` counts a freed outflow
  immediately.
- A freed outflow gets a `transfer_pair` question only if none was ever opened for
  it. `link_transfers`'s `reviewed` set already guarantees that.

### 5. History income counts every inflow (#19)

`accrual_by_month` (`src/moneta/views/cashflow.py:64-116`, bucketing at `:111-112`)
counts every unlinked positive transaction on a spend account as income. That
includes refunds, P2P money and one-off deposits. `power` counts only active,
non-discretionary inflow series (`src/moneta/views/power.py:97-99`). The history API
(`src/moneta/api.py:324-343`, `PowerMonth` at `:138-142`) and the CLI table
(`src/moneta/cli/main.py:250-258`) expose only the inflated income.

**Decision:**
- History income = transactions tagged to non-discretionary inflow series.
- Other inflows are shown separately as `other_inflows_cents`.
- Update the CLI history table and the docs.

#19's other proposals stay open: netting card refunds against spend, and pairing
pass-through money. The commit says `Refs #19`.

## File map

| File | Task | Change |
|---|---|---|
| `src/moneta/pipelines/transfers.py` | 1, 2, 3 | `PairScore`/`score_pair` in tenths, keyword rule, tie-break; `_P2P_RAILS`, whole-word `ach`; `_drop_stale_rule_links`, `TransferStats.unlinked` |
| `src/moneta/pipelines/reconcile.py` | 1 | link sort compares tenths |
| `src/moneta/cli/main.py` | 3, 4 | sync summary line for dropped links; history table "Other inflows" column |
| `src/moneta/views/cashflow.py` | 4 | `MonthFlows`, income by series, `accrual_income` → `accrual_inflows` |
| `src/moneta/api.py` | 4 | `PowerMonth.other_inflows_cents`, `/power/history` builds it |
| `tests/test_transfers.py` | 1, 2, 3 | score table, sync, ties, reconcile, rails, re-score |
| `tests/test_cli.py` | 3, 4 | dropped-link line; history table column |
| `tests/test_cashflow.py` | 4 | history income/other inflows |
| `tests/test_api.py` | 4 | `/power/history` other inflows |
| `tests/test_overlap_accounting.py`, `tests/test_overlap_preservation.py` | 4 | 4-field rows, rename |
| `docs/PRD.md`, `docs/user-guide.md`, `README.md`, `CLAUDE.md` | 1–4 | exact text in each task |

No files are created. There is no schema change.

---

### Task 1: Integer pair scores; a rule link needs a keyword

**Files:**
- Modify: `src/moneta/pipelines/transfers.py:49-50`, `:63-81`, `:210-219`, `:230-259`
- Modify: `src/moneta/pipelines/reconcile.py:59`
- Test: `tests/test_transfers.py`
- Docs: `docs/PRD.md`, `docs/user-guide.md`, `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `transfers._AUTO_LINK = 8` (int tenths).
  - `transfers.PairScore`, a frozen dataclass with `tenths: int`, `keyword: bool`, and
    the property `auto_links -> bool` (`keyword and tenths >= _AUTO_LINK`).
  - `transfers.score_pair(out: Transaction, inn: Transaction, out_type: AccountType) -> PairScore`.
  - `_Cand.score: PairScore`, replacing `_Cand.confidence`.
  - `_add_link(session, cand, method, tenths: int, used)`.

- [ ] **Step 1: Write the failing tests.**

Replace the import block at the top of `tests/test_transfers.py` (lines 1-9) with:

```python
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, ReviewItem, TransferLink
from moneta.pipelines.reconcile import reconcile_provider_overlap
from moneta.pipelines.run import run_sync
from moneta.pipelines.transfers import PairScore, link_transfers, score_pair
from moneta.views.power import power_report
from tests.conftest import RecordingAdapter
from tests.factories import make_account, make_txn

TODAY = date(2026, 7, 15)
```

Append to the end of `tests/test_transfers.py`:

```python
# (days apart, keyword, outflow leaves checking, score in tenths, rule-links)
_SCORE_TABLE = [
    (1, True, True, 10, True),
    (1, True, False, 9, True),
    (1, False, True, 8, False),  # reaches the threshold, but a rule link needs a keyword
    (1, False, False, 7, False),
    (3, True, True, 9, True),
    (3, True, False, 8, True),
    (3, False, True, 7, False),
    (3, False, False, 6, False),
    (4, True, True, 8, True),  # #46: float scoring summed this to 0.7999999999999999
    (4, True, False, 7, False),
    (4, False, True, 6, False),
    (4, False, False, 5, False),
]


@pytest.mark.parametrize(
    ("days_apart", "keyword", "from_checking", "tenths", "links"), _SCORE_TABLE
)
async def test_score_table_decides_rule_links(
    session: AsyncSession,
    days_apart: int,
    keyword: bool,
    from_checking: bool,
    tenths: int,
    links: bool,
) -> None:
    source = await make_account(
        session, type=AccountType.checking if from_checking else AccountType.credit
    )
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session,
        source,
        amount_cents=-10000,
        posted_on=date(2026, 7, 1),
        description="ONLINE TRANSFER TO SAVINGS" if keyword else "ACME STORE",
    )
    inn = await make_txn(
        session,
        savings,
        amount_cents=10000,
        posted_on=date(2026, 7, 1) + timedelta(days=days_apart),
        description="TRANSFER FROM CHECKING" if keyword else "CORNER CURRY",
    )
    assert score_pair(out, inn, source.type) == PairScore(tenths, keyword)
    stats = await link_transfers(session, llm=None)
    link = (await session.execute(select(TransferLink))).scalar_one_or_none()
    if links:
        assert stats.linked == 1
        assert link is not None
        assert (link.method, link.confidence) == ("rule", tenths / 10)
    else:
        assert link is None
        assert stats.review == 1  # a pair that doesn't auto-link is asked about, as before


async def test_card_payment_posting_four_days_apart_links_in_sync(session: AsyncSession) -> None:
    """#46: the card posts the payment credit four days before the checking debit.
    5 + 2 (keyword) + 1 (checking) is exactly the threshold; the payment must not
    also count as spend."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(session, type=AccountType.credit)
    inn = await make_txn(
        session,
        card,
        amount_cents=10000,
        posted_on=date(2026, 7, 1),
        description="PAYMENT RECEIVED",
    )
    out = await make_txn(
        session,
        checking,
        amount_cents=-10000,
        posted_on=date(2026, 7, 5),
        description="CARD PAYMENT",
    )
    await session.commit()
    report = await run_sync(session, [RecordingAdapter()], llm=None, today=TODAY)
    assert (report.transfers.linked, report.transfers.review) == (1, 0)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.method) == (out.id, inn.id, "rule")
    assert link.confidence == 0.8
    assert (await power_report(session, TODAY)).spent_so_far_cents == 0


async def test_equal_scores_break_ties_by_lower_outflow_id(session: AsyncSession) -> None:
    """Two outflows score 9 tenths for one inflow: same-day from a card, and two days
    from checking. Float sums made these 0.8999999999999999 and 0.9; integer tenths
    tie exactly, and the lower outflow id links."""
    card = await make_account(session, type=AccountType.credit)
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    first = await make_txn(
        session, card, amount_cents=-10000, posted_on=date(2026, 7, 3), description="TRANSFER OUT"
    )
    second = await make_txn(
        session,
        checking,
        amount_cents=-10000,
        posted_on=date(2026, 7, 1),
        description="TRANSFER OUT",
    )
    inn = await make_txn(
        session, savings, amount_cents=10000, posted_on=date(2026, 7, 3), description="TRANSFER IN"
    )
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review) == (1, 1)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id) == (first.id, inn.id)
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": second.id, "candidates": [inn.id]}


async def test_reconcile_keeps_the_older_of_two_equal_links(session: AsyncSession) -> None:
    """Two providers report one outflow, each observation rule-linked to a different
    inflow. Both links were saved at a nominal 9 tenths, the older one with float
    noise (0.8999999999999999); compared in tenths they tie and the older link stays."""
    bank = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=bank.id)
    savings = await make_account(session, source="simplefin", type=AccountType.savings)
    out = await make_txn(
        session, bank, amount_cents=-10000, posted_on=date(2026, 7, 1), description="TRANSFER OUT"
    )
    out_copy = await make_txn(
        session, alias, amount_cents=-10000, posted_on=date(2026, 7, 1), description="TRANSFER OUT"
    )
    first_in = await make_txn(
        session, savings, amount_cents=10000, posted_on=date(2026, 7, 1), description="TRANSFER IN"
    )
    second_in = await make_txn(
        session, savings, amount_cents=10000, posted_on=date(2026, 7, 1), description="TRANSFER IN"
    )
    session.add(
        TransferLink(
            outflow_id=out.id, inflow_id=first_in.id, confidence=0.8999999999999999, method="rule"
        )
    )
    session.add(
        TransferLink(outflow_id=out_copy.id, inflow_id=second_in.id, confidence=0.9, method="rule")
    )
    await session.commit()
    await reconcile_provider_overlap(session, TODAY)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert link.inflow_id == first_in.id
```

- [ ] **Step 2: Run the tests and confirm they fail.**

Run: `uv run pytest -q tests/test_transfers.py`
Expected: a collection error,
`ImportError: cannot import name 'PairScore' from 'moneta.pipelines.transfers'`.

Once the code exists, these tests would fail on the old scoring:
- the `1-True-True-10` row (stored `0.9999999999999999`);
- the `1-True-False-9` row;
- the `4-True-True-8` row;
- the sync, tie and reconcile tests.

The `1-False-True-8` row pins the new keyword rule.

- [ ] **Step 3: Implement.**

In `src/moneta/pipelines/transfers.py`, replace

```python
_MAX_DAYS = 6
_AUTO_LINK = 0.8
```

with

```python
_MAX_DAYS = 6
# Scores are integer tenths: summed float tenths drift (0.5 + 0.2 + 0.1 is
# 0.7999999999999999), so a pair meant to score exactly the threshold never linked.
_AUTO_LINK = 8
```

Replace the `_Cand` dataclass and the whole `_confidence` function (lines 63-81) with:

```python
@dataclass(frozen=True)
class PairScore:
    """A candidate pair's score in integer tenths, and whether a transfer keyword
    appears in either leg's description."""

    tenths: int
    keyword: bool

    @property
    def auto_links(self) -> bool:
        """A rule link needs a keyword match, whatever the score."""
        return self.keyword and self.tenths >= _AUTO_LINK


def score_pair(out: Transaction, inn: Transaction, out_type: AccountType) -> PairScore:
    """Base 5; +2 when the legs post 0-1 days apart, else +1 at 2-3 days; +2 for a
    transfer keyword; +1 when the outflow leaves a checking or savings account."""
    tenths = 5
    diff = abs((inn.posted_on - out.posted_on).days)
    if diff <= 1:
        tenths += 2
    elif diff <= 3:
        tenths += 1
    keyword = bool(_TRANSFER_PAT.search(out.description) or _TRANSFER_PAT.search(inn.description))
    if keyword:
        tenths += 2
    if out_type in (AccountType.checking, AccountType.savings):
        tenths += 1
    return PairScore(tenths, keyword)


@dataclass
class _Cand:
    outflow: Transaction
    inflow: Transaction
    score: PairScore
```

In `link_transfers`, replace the candidate build and the sort (lines 210-219):

```python
            conf = _confidence(
                out, inn, logical.accounts[logical.account_roots[out.account_id]].type
            )
            by_outflow.setdefault(out.id, []).append(_Cand(out, inn, conf))

    ordered = sorted(
        by_outflow.values(),
        key=lambda cands: (cands[0].outflow.id in selected, max(c.confidence for c in cands)),
        reverse=True,
    )
```

with

```python
            score = score_pair(
                out, inn, logical.accounts[logical.account_roots[out.account_id]].type
            )
            by_outflow.setdefault(out.id, []).append(_Cand(out, inn, score))

    # reviewed choices first, then the strongest group; equal scores are exact integer
    # ties, which the lower outflow id breaks
    ordered = sorted(
        by_outflow.values(),
        key=lambda cands: (
            cands[0].outflow.id not in selected,
            -max(c.score.tenths for c in cands),
            cands[0].outflow.id,
        ),
    )
```

Replace the three link paths (lines 230-243):

```python
        if out.id in selected:
            manual = choices[out.id].method == LinkMethod.manual
            cands[0].confidence = 1.0 if manual else 0.9
            _add_link(session, cands[0], LinkMethod.manual if manual else LinkMethod.llm, used)
            stats.linked += 1
            continue
        if len(original) == 1 and cands[0].confidence >= _AUTO_LINK:
            _add_link(session, cands[0], LinkMethod.rule, used)
            stats.linked += 1
            continue
        picked = await _llm_pick(llm, cands) if llm else None
        if picked is not None:
            _add_link(session, picked, LinkMethod.llm, used)
```

with

```python
        if out.id in selected:
            manual = choices[out.id].method == LinkMethod.manual
            method = LinkMethod.manual if manual else LinkMethod.llm
            _add_link(session, cands[0], method, 10 if manual else 9, used)
            stats.linked += 1
            continue
        if len(original) == 1 and cands[0].score.auto_links:
            _add_link(session, cands[0], LinkMethod.rule, cands[0].score.tenths, used)
            stats.linked += 1
            continue
        picked = await _llm_pick(llm, cands) if llm else None
        if picked is not None:
            _add_link(session, picked, LinkMethod.llm, picked.score.tenths, used)
```

Replace `_add_link` (lines 250-259) with:

```python
def _add_link(
    session: AsyncSession, cand: _Cand, method: LinkMethod, tenths: int, used: set[int]
) -> None:
    session.add(
        TransferLink(
            outflow_id=cand.outflow.id,
            inflow_id=cand.inflow.id,
            confidence=tenths / 10,
            method=method,
        )
    )
    used.update((cand.outflow.id, cand.inflow.id))
```

In `src/moneta/pipelines/reconcile.py`, replace line 59

```python
    for link in sorted(links, key=lambda row: (rank[row.method], -row.confidence, row.id)):
```

with

```python
    # compare stored confidence in tenths: links saved before integer scoring carry
    # float noise (0.8999999999999999), which must not outrank an equal, older link
    for link in sorted(
        links, key=lambda row: (rank[row.method], -round(row.confidence * 10), row.id)
    ):
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_transfers.py tests/test_transaction_overlap.py`
Expected: all pass. That includes the existing greedy-loser and
ambiguous-then-one-remaining tests, which are unchanged.

- [ ] **Step 5: Update the docs.**

In `docs/PRD.md` §6.2, replace the start of the **Transfer dedup** row. Old:

```
| **Transfer dedup** | Candidate pairs scored on amount, date proximity, account direction. High-confidence pairs auto-link; ambiguous ones escalate to LLM/review. Both legs
```

New:

```
| **Transfer dedup** | Candidates are an outflow and an inflow of opposite amounts on two accounts in one currency, posted at most 6 days apart, scored in integer tenths: 5, +2 when the legs post 0–1 days apart or +1 at 2–3 days, +2 for a transfer keyword in either description, +1 when the outflow leaves a checking or savings account. A lone candidate auto-links by rule when it scores at least 8 **and** has a keyword; everything else escalates to LLM/review. Both legs
```

The rest of the row is unchanged.

In the **Transfer dedup edge cases** row, replace `instead of a server error. |` with:

```
instead of a server error. Groups are taken best score first; equal scores are exact integer ties, and the lower outflow id goes first. |
```

In `docs/PRD.md` §7, insert this row directly under the table's header separator
(`|---|---|---|`), above every existing row:

```
| 2026-10-14 | **Transfer scores in integer tenths** | Transfer pairs are scored in integer tenths instead of summed floats, so a pair scoring exactly the threshold (a card payment posting 4 days after the bank debit) now links, and equal scores tie exactly, broken by the lower outflow id (#46). A rule link also needs a transfer keyword in either description, whatever the score; a keyword-less pair goes to the LLM or review instead. Provider-overlap reconciliation compares stored link confidence in tenths, so float noise in an older link can't decide which of two equal links survives. |
```

In `docs/user-guide.md`, insert this subsection directly above the
``### `moneta networth` `` heading:

```markdown
### How transfers are matched

A transfer between two of your accounts (checking → savings, checking → a card payment)
is linked, so neither leg counts as income or spend. A candidate is an outflow and an
inflow of exactly opposite amounts, on two different accounts in the same currency,
posted at most 6 days apart. Each candidate pair is scored in points:

| Points | When |
|---|---|
| 5 | every candidate |
| +2 | the legs post 0–1 days apart (+1 at 2–3 days; nothing at 4–6) |
| +2 | either description has transfer wording: `payment`, `pymt`, `transfer`, `xfer`, `ach`, `autopay`, `epay`, `billpay` or `deposit` (any case) |
| +1 | the outflow leaves a checking or savings account |

An outflow with exactly one candidate links automatically when the pair scores 8 or more
**and** has transfer wording. A same-amount pair with no transfer wording is as often a
coincidence as a transfer, so it is never linked on points alone. Every other case goes
to the LLM when one is configured, else to `moneta review` as a transfer question. When
two outflows want the same inflow, the higher score wins and an equal score goes to the
outflow Moneta stored first; the other outflow is asked about.
```

In `README.md`, replace the **Transfer dedup** bullet

```
- **Transfer dedup** — candidate pairs scored on amount, date proximity, and
  account direction; high-confidence pairs auto-link, ambiguous ones escalate.
  Human decisions remain effective across provider aliases and later automatic review.
```

with

```
- **Transfer dedup** — opposite-amount pairs up to 6 days apart, scored on date
  proximity, transfer wording and account type; a lone pair that scores high *and*
  carries transfer wording auto-links, the rest escalate.
  Human decisions remain effective across provider aliases and later automatic review.
```

In `CLAUDE.md`, the **Transfer-link semantics** bullet ends with
`— extend it rather than re-joining TransferLink at call sites.` Append this to that
same line:

```
 Transfer pair scores are integer tenths (`transfers.score_pair` → `PairScore`) — never sum float confidences, `0.5 + 0.2 + 0.1 < 0.8`; a rule link needs `PairScore.auto_links` (score ≥ `_AUTO_LINK` = 8 **and** a keyword), and `TransferLink.confidence` stores tenths ÷ 10.
```

- [ ] **Step 6: Run the full gate.**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`
Expected: all pass, pristine output.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/pipelines/transfers.py src/moneta/pipelines/reconcile.py tests/test_transfers.py docs/PRD.md docs/user-guide.md README.md CLAUDE.md
git commit -s -m "fix: score transfer pairs in integer tenths; a rule link needs a keyword" -m "Refs #46."
```

---

### Task 2: Keyword wording: P2P rails and a whole-word `ach`

**Files:**
- Modify: `src/moneta/pipelines/transfers.py` (`_TRANSFER_PAT` block, `score_pair`)
- Test: `tests/test_transfers.py`
- Docs: `docs/PRD.md`, `docs/user-guide.md`, `CLAUDE.md`. `README.md` doesn't list the
  keywords, so it is unchanged.

**Interfaces:**
- Consumes: `score_pair` and `PairScore` from Task 1.
- Produces:
  - `transfers._P2P_RAILS: re.Pattern[str]`;
  - `transfers._has_keyword(description: str) -> bool`;
  - `_TRANSFER_PAT` with `\bach\b`.

  `score_pair` uses `_has_keyword`; its signature is unchanged.

- [ ] **Step 1: Write the failing tests.**

In `tests/test_transfers.py`, change the models import to:

```python
from moneta.models import AccountType, ReviewItem, Transaction, TransferLink
```

Append:

```python
@pytest.mark.parametrize(
    "description",
    [
        "Zelle payment from JANE DOE 1234",
        "ZELLE PAYMENT TO JANE DOE",
        "zelle  payment   from jane doe",
        "VENMO PAYMENT 1234",
        "PayPal Inst Xfer JANE DOE",
    ],
)
def test_p2p_rail_wording_is_not_a_transfer_keyword(description: str) -> None:
    out = Transaction(posted_on=date(2026, 7, 1), description="Acme Wallet: Add Money")
    inn = Transaction(posted_on=date(2026, 7, 1), description=description)
    assert score_pair(out, inn, AccountType.credit) == PairScore(7, False)


def test_text_after_a_rail_still_counts() -> None:
    out = Transaction(posted_on=date(2026, 7, 1), description="ACME STORE")
    inn = Transaction(posted_on=date(2026, 7, 1), description="Zelle payment from JANE DOE ACH")
    assert score_pair(out, inn, AccountType.credit).keyword


@pytest.mark.parametrize(
    ("description", "keyword"),
    [
        ("CORNER CAFE ELMTON BEACH CA", False),
        ("buy 2 shares of Acme for $10.00 each", False),
        ("ACME COACH LINES", False),
        ("ACME BANK ACH PMT", True),
        ("ONETIMEPAYMENT", True),
        ("ACME PAYMENTS PPD", True),
    ],
)
def test_ach_must_be_a_whole_word(description: str, keyword: bool) -> None:
    """Only `ach` is matched as a whole word ("BEACH", "COACH" and "each" are not
    transfers); every other keyword stays a substring, so glued descriptors such as
    "ONETIMEPAYMENT" still count."""
    out = Transaction(posted_on=date(2026, 7, 1), description="ACME STORE")
    inn = Transaction(posted_on=date(2026, 7, 1), description=description)
    assert score_pair(out, inn, AccountType.credit).keyword is keyword


async def test_p2p_inflow_does_not_link_to_a_wallet_top_up(session: AsyncSession) -> None:
    """A friend's Zelle inflow and an unrelated same-amount card outflow on one day
    must not pair: the rail's own "payment" is not a transfer keyword."""
    card = await make_account(session, type=AccountType.credit)
    checking = await make_account(session, type=AccountType.checking)
    out = await make_txn(
        session,
        card,
        amount_cents=-30000,
        posted_on=date(2026, 7, 1),
        description="Acme Wallet: Add Money",
    )
    inn = await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 1),
        description="Zelle payment from JANE DOE 1234",
    )
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review) == (0, 1)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}
```

- [ ] **Step 2: Run the tests and confirm they fail.**

Run: `uv run pytest -q tests/test_transfers.py`
Expected:
- The five `test_p2p_rail_wording_is_not_a_transfer_keyword` cases fail with
  `PairScore(tenths=9, keyword=True) != PairScore(tenths=7, keyword=False)`.
- `test_p2p_inflow_does_not_link_to_a_wallet_top_up` fails on `(1, 0) != (0, 1)`.
- `test_ach_must_be_a_whole_word` fails for its three `False` cases ("BEACH", "each",
  "COACH").
- `test_text_after_a_rail_still_counts` already passes, and so do the three `True`
  cases of `test_ach_must_be_a_whole_word`. They guard against over-stripping and
  over-anchoring.

- [ ] **Step 3: Implement.**

In `src/moneta/pipelines/transfers.py`, replace

```python
_TRANSFER_PAT = re.compile(
    r"payment|pymt|transfer|xfer|ach|autopay|epay|billpay|deposit", re.IGNORECASE
)
```

with

```python
# Substring matches on purpose: descriptors glue words ("ONETIMEPAYMENT"). Only "ach"
# is a whole word, because as a substring it hides in "BEACH", "COACH" and "each".
_TRANSFER_PAT = re.compile(
    r"payment|pymt|transfer|xfer|\bach\b|autopay|epay|billpay|deposit", re.IGNORECASE
)
```

Directly below that statement, add:

```python
# A P2P rail names a person, not one of your accounts: in "Zelle payment from JANE
# DOE" the word "payment" belongs to the rail. The rail phrase is removed before the
# keyword search, so it can't vouch for a transfer on its own.
_P2P_RAILS = re.compile(
    r"zelle\s+payment\s+(?:from|to)|venmo\s+payment|paypal\s+inst\s+xfer", re.IGNORECASE
)
```

Directly above `def score_pair`, add:

```python
def _has_keyword(description: str) -> bool:
    return _TRANSFER_PAT.search(_P2P_RAILS.sub(" ", description)) is not None
```

In `score_pair`, replace

```python
    keyword = bool(_TRANSFER_PAT.search(out.description) or _TRANSFER_PAT.search(inn.description))
```

with

```python
    keyword = _has_keyword(out.description) or _has_keyword(inn.description)
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_transfers.py`
Expected: all pass.

- [ ] **Step 5: Update the docs.**

In `docs/PRD.md` §6.2 **Transfer dedup** row, replace
`+2 for a transfer keyword in either description,` with:

```
+2 for a transfer keyword in either description (P2P rail wording — `Zelle payment from`/`to`, `Venmo payment`, `PayPal Inst Xfer` — is removed first; `ach` counts only as a whole word, every other keyword also inside a longer word),
```

In `docs/PRD.md` §7, insert directly under the header separator, above the Task 1 row:

```
| 2026-10-14 | **P2P rails and stray "ach" aren't transfer wording** | Payment-app rail wording (`Zelle payment from`/`to`, `Venmo payment`, `PayPal Inst Xfer`, any case or spacing) is removed before the transfer-keyword search, so a friend's P2P inflow no longer rule-links to an unrelated same-amount card outflow on the rail's own "payment" (#46). The keyword `ach` now matches only as a whole word, so "BEACH", "COACH" or "each" no longer count; every other keyword is still found inside longer words ("ONETIMEPAYMENT"). |
```

In `docs/user-guide.md` **How transfers are matched**, insert this paragraph directly
below the points table, above "An outflow with exactly one candidate…":

```markdown
Payment-app rail wording doesn't count as transfer wording: `Zelle payment from`/`to`,
`Venmo payment` and `PayPal Inst Xfer` (any case or spacing) name a person, not one of
your accounts, so that phrase is ignored before the search. Other words in the same
description still count.
```

In the same subsection's points table, replace the transfer-wording row

```
| +2 | either description has transfer wording: `payment`, `pymt`, `transfer`, `xfer`, `ach`, `autopay`, `epay`, `billpay` or `deposit` (any case) |
```

with

```
| +2 | either description has transfer wording: `payment`, `pymt`, `transfer`, `xfer`, `ach`, `autopay`, `epay`, `billpay` or `deposit` (any case; every word but `ach` also counts inside a longer word, e.g. `ONETIMEPAYMENT`, while `ach` must stand alone, so `BEACH` doesn't count) |
```

In `CLAUDE.md` **Transfer-link semantics**, replace `**and** a keyword)` with:

```
**and** a keyword, searched after `_P2P_RAILS` strips P2P rail wording such as `Zelle payment from`; keywords are substring matches except a whole-word `ach`, because descriptors glue words like `ONETIMEPAYMENT`)
```

- [ ] **Step 6: Run the full gate.**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`
Expected: all pass, pristine output.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/pipelines/transfers.py tests/test_transfers.py docs/PRD.md docs/user-guide.md CLAUDE.md
git commit -s -m "fix: P2P rail wording and a stray 'ach' no longer count as transfer keywords" -m "Refs #46."
```

---

### Task 3: Re-score rule links every run

**Files:**
- Modify: `src/moneta/pipelines/transfers.py` (import, `TransferStats`, new
  `_drop_stale_rule_links`, start of `link_transfers`)
- Modify: `src/moneta/cli/main.py:144-150` (sync summary)
- Test: `tests/test_transfers.py`, `tests/test_cli.py`
- Docs: `docs/PRD.md`, `docs/user-guide.md`, `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes:
  - `score_pair` and `PairScore.auto_links` (Tasks 1–2);
  - `LogicalData` (`moneta.logical`), with `transactions`, `transaction_roots`,
    `account_roots` and `accounts`.
- Produces:
  - `TransferStats.unlinked: int = 0`, which reaches the API as
    `SyncReport.transfers.unlinked`;
  - `_drop_stale_rule_links(session: AsyncSession, logical: LogicalData) -> int`;
  - the CLI sync line `Dropped N stale transfer link(s).`

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_transfers.py` (`Transaction` was imported in Task 2):

```python
async def _wallet_and_p2p(session: AsyncSession) -> tuple[Transaction, Transaction]:
    """A card's wallet top-up and a friend's same-day, same-amount Zelle inflow."""
    card = await make_account(session, type=AccountType.credit)
    checking = await make_account(session, type=AccountType.checking)
    out = await make_txn(
        session,
        card,
        amount_cents=-30000,
        posted_on=date(2026, 7, 10),
        description="Acme Wallet: Add Money",
    )
    inn = await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 10),
        description="Zelle payment from JANE DOE 1234",
    )
    return out, inn


async def test_stale_rule_link_is_dropped_and_asked_about(session: AsyncSession) -> None:
    out, inn = await _wallet_and_p2p(session)
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=0.9, method="rule"))
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 1, 1)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def test_qualifying_rule_link_stays_and_is_restamped_in_tenths(
    session: AsyncSession,
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session,
        checking,
        amount_cents=-50000,
        posted_on=date(2026, 7, 1),
        description="ONLINE TRANSFER TO SAVINGS",
    )
    inn = await make_txn(
        session,
        savings,
        amount_cents=50000,
        posted_on=date(2026, 7, 1),
        description="TRANSFER FROM CHECKING",
    )
    session.add(
        TransferLink(
            outflow_id=out.id, inflow_id=inn.id, confidence=0.9999999999999999, method="rule"
        )
    )
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 0, 0)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.confidence) == (out.id, inn.id, 1.0)


@pytest.mark.parametrize(("method", "confidence"), [("manual", 1.0), ("llm", 0.9)])
async def test_human_and_llm_links_are_never_rescored(
    session: AsyncSession, method: str, confidence: float
) -> None:
    out, inn = await _wallet_and_p2p(session)
    session.add(
        TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=confidence, method=method)
    )
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 0, 0)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.method) == (out.id, inn.id, method)


async def test_freed_leg_re_pairs_in_the_same_run(session: AsyncSession) -> None:
    """Dropping the stale link frees the inflow for a genuine same-day transfer from
    savings, which links by rule in the same run; the wallet top-up loses that inflow
    to the stronger pair and is asked about."""
    out, inn = await _wallet_and_p2p(session)
    savings = await make_account(session, type=AccountType.savings)
    move = await make_txn(
        session,
        savings,
        amount_cents=-30000,
        posted_on=date(2026, 7, 10),
        description="TRANSFER TO CHECKING",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=0.9, method="rule"))
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (1, 1, 1)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.method) == (move.id, inn.id, "rule")
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def test_rescore_judges_a_duplicate_observation_as_its_survivor(
    session: AsyncSession,
) -> None:
    """The stale link points at the second provider's copy of the Zelle inflow. The
    re-score follows it to the surviving observation, the one the linker scores."""
    card = await make_account(session, type=AccountType.credit, source="simplefin")
    checking = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=checking.id)
    out = await make_txn(
        session,
        card,
        amount_cents=-30000,
        posted_on=date(2026, 7, 10),
        description="Acme Wallet: Add Money",
    )
    inn = await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 10),
        description="Zelle payment from JANE DOE 1234",
    )
    copy = await make_txn(
        session,
        alias,
        amount_cents=30000,
        posted_on=date(2026, 7, 10),
        description="ZELLE PAYMENT FROM JANE DOE",
        duplicate_of_id=inn.id,
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=copy.id, confidence=0.9, method="rule"))
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 1, 1)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def test_sync_drops_a_stale_link_before_spend_is_counted(session: AsyncSession) -> None:
    """In a full sync the re-score runs inside the transfers stage, so the freed
    wallet top-up counts as spend in the same sync's numbers."""
    out, inn = await _wallet_and_p2p(session)
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=0.9, method="rule"))
    await session.commit()
    report = await run_sync(session, [RecordingAdapter()], llm=None, today=TODAY)
    assert (report.transfers.unlinked, report.transfers.linked) == (1, 0)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    assert (await power_report(session, TODAY)).spent_so_far_cents == 30000
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert (item.kind, item.status) == ("transfer_pair", "open")
```

Append to `tests/test_cli.py` (`pytest`, `Path`, `Any`, `_isolate`, `runner` and `app`
are already in scope there):

```python
@pytest.mark.parametrize(
    ("unlinked", "line"),
    [(2, "Dropped 2 stale transfer links."), (1, "Dropped 1 stale transfer link."), (0, None)],
)
def test_sync_reports_dropped_transfer_links(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch, unlinked: int, line: str | None
) -> None:
    _isolate(monkeypatch, tmp_path)

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if path == "/sync":
            return {
                "ingest": {"new_transactions": 0},
                "transfers": {"linked": 0, "review": 0, "unlinked": unlinked},
                "recurring": {"new_series": 0},
                "events": 0,
                "auto_resolved": 0,
                "verify": {"verified": 0, "flagged": 0},
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    if line is None:
        assert "Dropped" not in result.output
    else:
        assert line in result.output
```

- [ ] **Step 2: Run the tests and confirm they fail.**

Run: `uv run pytest -q tests/test_transfers.py tests/test_cli.py`
Expected:
- The new transfer tests fail. `TransferStats` has no `unlinked` attribute, and the
  stale link is still there.
- The CLI cases with `unlinked` 2 and 1 fail because the line is missing. The 0 case
  passes.

- [ ] **Step 3: Implement.**

In `src/moneta/pipelines/transfers.py`, change the logical import to:

```python
from moneta.logical import LogicalData, load_logical_data
```

Replace `TransferStats` with:

```python
class TransferStats(BaseModel):
    linked: int = 0
    review: int = 0
    unlinked: int = 0  # rule links dropped because they no longer auto-link
```

Directly above `async def link_transfers`, add:

```python
async def _drop_stale_rule_links(session: AsyncSession, logical: LogicalData) -> int:
    """Re-score every rule link and delete the ones that no longer auto-link, so a
    link made under older scoring can't outlive it. Links a person or the LLM chose
    are never touched. Endpoints are judged as their surviving occurrence, the same
    transactions the linker scores; a link with a missing leg is left to reconcile."""
    by_id = {t.id: t for t in logical.transactions}
    dropped = 0
    rule_links = await session.scalars(
        select(TransferLink).where(TransferLink.method == LinkMethod.rule)
    )
    for link in list(rule_links):
        out = by_id.get(logical.transaction_roots.get(link.outflow_id, link.outflow_id))
        inn = by_id.get(logical.transaction_roots.get(link.inflow_id, link.inflow_id))
        if out is None or inn is None:
            continue
        score = score_pair(out, inn, logical.accounts[logical.account_roots[out.account_id]].type)
        if score.auto_links:
            link.confidence = score.tenths / 10
        else:
            await session.delete(link)
            dropped += 1
    await session.flush()
    return dropped
```

At the start of `link_transfers`, replace

```python
    stats = TransferStats()
    logical = await load_logical_data(session)
```

with

```python
    stats = TransferStats()
    logical = await load_logical_data(session)
    # first, so legs freed here are matched again in this same run
    stats.unlinked = await _drop_stale_rule_links(session, logical)
```

The `linked_ids` loop below it reads `TransferLink` after the flush, so dropped links
no longer exclude their legs.

In `src/moneta/cli/main.py` `sync`, insert directly after the `Synced: …` `console.print(...)`
call and before `if report["auto_resolved"]:`:

```python
    if unlinked := report["transfers"].get("unlinked"):
        console.print(f"Dropped {_plural(unlinked, 'stale transfer link')}.")
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_transfers.py tests/test_cli.py tests/test_overlap_preservation.py`
Expected: all pass.

Two mutation checks:
- Drop the `transaction_roots` mapping in `_drop_stale_rule_links`:
  `test_rescore_judges_a_duplicate_observation_as_its_survivor` must fail.
- Drop the `method == rule` filter: `test_human_and_llm_links_are_never_rescored` must
  fail.

Restore both changes afterwards.

- [ ] **Step 5: Update the docs.**

In `docs/PRD.md` §6.2 **Transfer dedup** row, replace
`everything else escalates to LLM/review.` with:

```
everything else escalates to LLM/review. Every run first re-scores the existing rule links and drops those that no longer qualify, then matches the freed legs again; manual and LLM links are never re-scored.
```

In `docs/PRD.md` §7, insert directly under the header separator, above the Task 2 row:

```
| 2026-10-14 | **Stale rule links are re-scored** | Every sync and `moneta reconcile` re-score each existing rule link with the current rules and delete the ones that no longer auto-link (for example one made before the keyword and P2P-rail rules), then match the freed legs again in the same run, before financing, recurring detection and the views read the links; manual and LLM links are never touched (#46). `SyncReport.transfers.unlinked` counts the drops, and `moneta sync` prints `Dropped N stale transfer links.` |
```

In `docs/user-guide.md` **How transfers are matched**, append this paragraph at the end
of the subsection:

```markdown
Every sync, and `moneta reconcile`, first re-checks each link it made automatically
against these rules and drops the ones that no longer qualify, then matches the freed
transactions again in the same run. A link made under older rules therefore can't keep
a purchase out of spent-so-far. Links you chose in `moneta review`, and links the LLM
picked, are never re-checked.
```

In `docs/user-guide.md` **Syncing**, insert this paragraph directly after the paragraph
that ends `…out of the client's view.`:

```markdown
When a sync drops automatic transfer links that no longer qualify (see
[How transfers are matched](#how-transfers-are-matched)), the summary adds a line such
as `Dropped 1 stale transfer link.` The freed transactions are matched again, or asked
about, in that same sync.
```

In `README.md`, replace the **Transfer dedup** bullet (as left by Task 1) with:

```
- **Transfer dedup** — opposite-amount pairs up to 6 days apart, scored on date
  proximity, transfer wording and account type; a lone pair that scores high *and*
  carries transfer wording auto-links, the rest escalate. Automatic links are
  re-checked every sync and dropped when they no longer qualify.
  Human decisions remain effective across provider aliases and later automatic review.
```

In `CLAUDE.md` **Transfer-link semantics**, append to the same line, after the Task 1
sentence:

```
 `link_transfers` starts by re-scoring every `rule` link with that same `score_pair` and deleting the ones that no longer auto-link — never a `manual` or `llm` link — so the linker and the re-score can't disagree, and the freed legs are matched again in the same run, before financing, recurring and the views read the links.
```

- [ ] **Step 6: Run the full gate.**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`
Expected: all pass, pristine output.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/pipelines/transfers.py src/moneta/cli/main.py tests/test_transfers.py tests/test_cli.py docs/PRD.md docs/user-guide.md README.md CLAUDE.md
git commit -s -m "fix: re-score rule transfer links every run and drop stale ones" -m "Closes #46."
```

---

### Task 4: History income is detected income; other inflows shown beside it

**Files:**
- Modify: `src/moneta/views/cashflow.py` (imports, new `MonthFlows`, `_accrual`
  docstring, `accrual_income` → `accrual_inflows`, `accrual_by_month`)
- Modify: `src/moneta/api.py:138-142` (`PowerMonth`) and `:324-343` (`power_history`)
- Modify: `src/moneta/cli/main.py:238-260` (power `--history`)
- Test: `tests/test_cashflow.py`, `tests/test_api.py`, `tests/test_cli.py`,
  `tests/test_overlap_accounting.py`, `tests/test_overlap_preservation.py`
- Docs: `docs/PRD.md`, `docs/user-guide.md`, `README.md`

**Interfaces:**
- Consumes: `RecurringSeries.direction`, `RecurringSeries.discretionary`,
  `Transaction.series_id`.
- Produces:
  - `cashflow.MonthFlows`, a `NamedTuple` with `month: str`, `income_cents: int`,
    `other_inflows_cents: int` and `spend_cents: int`;
  - `accrual_by_month(...) -> list[MonthFlows]`;
  - `accrual_inflows(session, start, end, links=None, primary=None) -> int`, which
    replaces `accrual_income`;
  - `PowerMonth.other_inflows_cents: int`.

- [ ] **Step 1: Write the failing tests.**

In `tests/test_cashflow.py`, replace the three import lines

```python
from moneta.models import Account, AccountType, TransferLink
from moneta.views.cashflow import accrual_by_month, accrual_income, accrual_spend, cash_out
from tests.factories import make_account, make_txn
```

with

```python
from moneta.models import Account, AccountType, Direction, SeriesStatus, TransferLink
from moneta.views.cashflow import accrual_by_month, accrual_inflows, accrual_spend, cash_out
from tests.factories import make_account, make_series, make_txn
```

Then replace everything from `async def test_accrual_income_counts_paycheck_inflow` to
the end of the file with:

```python
async def test_accrual_inflows_counts_every_unlinked_inflow(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 2),
        description="ACME CORP PAYROLL",
    )
    assert await accrual_inflows(session, date(2026, 7, 1), date(2026, 7, 31)) == 250000


async def test_accrual_inflows_excludes_linked_inflow(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    # the only positive-amount txn in this fixture is the CC payment's inflow leg
    # (credit account, +8000, transfer-linked) — it must not count as income.
    assert await accrual_inflows(session, date(2026, 7, 1), date(2026, 7, 31)) == 0


async def _payroll_series(session: AsyncSession, **kw: object) -> int:
    series = await make_series(
        session,
        merchant="Acme Payroll",
        direction=Direction.inflow,
        expected_cents=250000,
        **kw,
    )
    return series.id


async def test_accrual_by_month_buckets_multiple_months(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-5000, posted_on=date(2026, 7, 10), description="GROCERY"
    )
    await make_txn(
        session,
        checking,
        amount_cents=200000,
        posted_on=date(2026, 6, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-4000, posted_on=date(2026, 6, 20), description="GROCERY"
    )
    rows = await accrual_by_month(session, months=2, today=date(2026, 7, 20))
    assert rows == [
        ("2026-07", 250000, 0, 5000),
        ("2026-06", 200000, 0, 4000),
    ]


async def test_history_income_is_detected_income_only(session: AsyncSession) -> None:
    """#19: a friend's Zelle inflow is another inflow, not income; a paycheck tagged
    to a payroll series is income."""
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 1),
        description="ACME PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 3),
        description="Zelle payment from JANE DOE 1234",
    )
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 250000, 30000, 0)]
    assert (rows[0].income_cents, rows[0].other_inflows_cents) == (250000, 30000)


async def test_card_refund_is_another_inflow_not_income(session: AsyncSession) -> None:
    credit = await make_account(session, type=AccountType.credit)
    await make_txn(
        session, credit, amount_cents=-5000, posted_on=date(2026, 7, 2), description="ACME STORE"
    )
    await make_txn(
        session,
        credit,
        amount_cents=5000,
        posted_on=date(2026, 7, 9),
        description="ACME STORE REFUND",
    )
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 5000, 5000)]


async def test_ended_paycheck_series_counts_in_the_months_it_paid(session: AsyncSession) -> None:
    """A former employer's series has ended; its paychecks were income in their months."""
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session, status=SeriesStatus.ended)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 5, 1),
        description="ACME PAYROLL",
        series_id=payroll,
    )
    rows = await accrual_by_month(session, months=3, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 0, 0), ("2026-06", 0, 0, 0), ("2026-05", 250000, 0, 0)]


async def test_discretionary_inflow_series_is_not_income(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    habit = await _payroll_series(session, discretionary=True)
    await make_txn(
        session,
        checking,
        amount_cents=250000,
        posted_on=date(2026, 7, 1),
        description="ACME PAYROLL",
        series_id=habit,
    )
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 250000, 0)]


async def test_accrual_by_month_excludes_linked_txns(session: AsyncSession) -> None:
    await _cc_purchase_and_payment(session)
    # same fixture as test_accrual_counts_cc_purchases_not_payments/
    # test_accrual_inflows_excludes_linked_inflow: RESTAURANT counts as spend,
    # the CC PAYMENT's linked legs count nowhere.
    rows = await accrual_by_month(session, months=1, today=date(2026, 7, 20))
    assert rows == [("2026-07", 0, 0, 8000)]


async def test_accrual_by_month_year_boundary_and_equivalence(session: AsyncSession) -> None:
    """3-month trailing window crossing the Dec/Jan year boundary: buckets land in the
    right calendar month (label year rolls back correctly), and each bucket agrees
    exactly with accrual_inflows/accrual_spend called directly over that month's
    [month_start, month_end] window — the single-SELECT batching in accrual_by_month
    must not diverge from the per-month accessors it's meant to replace."""
    checking = await make_account(session, type=AccountType.checking)
    payroll = await _payroll_series(session)
    # Nov 2025
    await make_txn(
        session,
        checking,
        amount_cents=200000,
        posted_on=date(2025, 11, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-3000, posted_on=date(2025, 11, 20), description="GROCERY"
    )
    # Dec 2025
    await make_txn(
        session,
        checking,
        amount_cents=210000,
        posted_on=date(2025, 12, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=1500, posted_on=date(2025, 12, 9), description="REFUND"
    )
    await make_txn(
        session, checking, amount_cents=-4500, posted_on=date(2025, 12, 24), description="GIFTS"
    )
    # Jan 2026 — the "today" month, still in progress
    await make_txn(
        session,
        checking,
        amount_cents=220000,
        posted_on=date(2026, 1, 5),
        description="PAYROLL",
        series_id=payroll,
    )
    await make_txn(
        session, checking, amount_cents=-6000, posted_on=date(2026, 1, 10), description="RENT"
    )

    today = date(2026, 1, 15)
    rows = await accrual_by_month(session, months=3, today=today)
    assert rows == [
        ("2026-01", 220000, 0, 6000),
        ("2025-12", 210000, 1500, 4500),
        ("2025-11", 200000, 0, 3000),
    ]

    for label, income_cents, other_inflows_cents, spend_cents in rows:
        year_s, month_s = label.split("-")
        start, end = month_bounds(date(int(year_s), int(month_s), 1))
        assert await accrual_inflows(session, start, end) == income_cents + other_inflows_cents
        assert await accrual_spend(session, start, end) == spend_cents
```

In `tests/test_overlap_accounting.py`:
- In the `moneta.views.cashflow` import, rename `accrual_income` to `accrual_inflows`.
- Replace `assert await accrual_income(session, start, end) == 200000` with
  `assert await accrual_inflows(session, start, end) == 200000`.
- Replace `== ("2025-07", 0, 1700)` with `== ("2025-07", 0, 0, 1700)`.
- Replace

  ```python
      assert await accrual_by_month(session, 2, TODAY) == [("2026-07", 0, 0), ("2026-06", 0, 2000)]
  ```

  with

  ```python
      assert await accrual_by_month(session, 2, TODAY) == [
          ("2026-07", 0, 0, 0),
          ("2026-06", 0, 0, 2000),
      ]
  ```

In `tests/test_overlap_preservation.py`, rename every `accrual_income` to
`accrual_inflows`: the import, line 149 and line 217. The values are unchanged; those
fixtures carry no series.

In `tests/test_api.py` `test_power_history_multi_month_rows_newest_first`:
- Replace `from moneta.models import AccountType` with
  `from moneta.models import AccountType, Direction`.
- Replace the current-month block

  ```python
          checking = await make_account(session, type=AccountType.checking)
          # this (current, partial) month: one paycheck, one purchase
          await make_txn(
              session, checking, amount_cents=300000, posted_on=today, description="PAYROLL"
          )
  ```

  with

  ```python
          checking = await make_account(session, type=AccountType.checking)
          payroll = await make_series(
              session, merchant="Acme Payroll", direction=Direction.inflow, expected_cents=300000
          )
          # this (current, partial) month: one paycheck, a friend's Zelle, one purchase
          await make_txn(
              session,
              checking,
              amount_cents=300000,
              posted_on=today,
              description="PAYROLL",
              series_id=payroll.id,
          )
          await make_txn(
              session,
              checking,
              amount_cents=30000,
              posted_on=today,
              description="Zelle payment from JANE DOE 1234",
          )
  ```

- In last month's paycheck `make_txn(...)` (the one with `amount_cents=250000`), add
  `series_id=payroll.id,` after `description="PAYROLL",`.
- After `assert rows[0]["income_cents"] == 300000`, add
  `assert rows[0]["other_inflows_cents"] == 30000  # shown, but not income`.
- After `assert rows[1]["income_cents"] == 250000`, add
  `assert rows[1]["other_inflows_cents"] == 0`.
- Every other assert is unchanged, including `net_cents == 295000`.

The test anchors on `date.today()`, as the endpoint does.

In `tests/test_cli.py`, replace `test_power_history_table_renders_month_income_spend_net`
with:

```python
def test_power_history_table_renders_month_income_other_spend_net(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        assert (method, path, params) == ("GET", "/power/history", {"months": 3})
        return [
            {
                "month": "2026-07",
                "income_cents": 300000,
                "other_inflows_cents": 12345,
                "spend_cents": 5000,
                "net_cents": 295000,
            },
            {
                "month": "2026-06",
                "income_cents": 250000,
                "other_inflows_cents": 0,
                "spend_cents": 260000,
                "net_cents": -10000,
            },
            {
                "month": "2026-05",
                "income_cents": 250000,
                "other_inflows_cents": 0,
                "spend_cents": 4000,
                "net_cents": 246000,
            },
        ]

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["power", "--history", "3"])
    assert result.exit_code == 0
    for header in ("Month", "Income", "Other inflows", "Spend", "Net"):
        assert header in result.output
    assert "2026-07" in result.output
    assert "$3000.00" in result.output  # income: fmt_money
    assert "$123.45" in result.output  # other inflows: fmt_money
    assert "-$50.00" in result.output  # spend: fmt_outflow(5000)
    assert "$2950.00" in result.output  # net: fmt_money, positive
    assert "-$100.00" in result.output  # net: fmt_money, negative month
    assert "Spending power" not in result.output
```

- [ ] **Step 2: Run the tests and confirm they fail.**

Run: `uv run pytest -q tests/test_cashflow.py tests/test_overlap_accounting.py tests/test_overlap_preservation.py`
Expected: all three files fail to collect with
`ImportError: cannot import name 'accrual_inflows'`.

Then run: `uv run pytest -q tests/test_api.py tests/test_cli.py -k power_history`
Expected: exactly two failures.
- `test_power_history_multi_month_rows_newest_first` fails with `330000 != 300000`
  (the Zelle inflow is counted as income).
- `test_power_history_table_renders_month_income_other_spend_net` fails on the
  missing `Other inflows` header.

- [ ] **Step 3: Implement.**

In `src/moneta/views/cashflow.py`, replace the imports (lines 1-11) with:

```python
from datetime import date
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.cadence import month_bounds
from moneta.logical import load_logical_data
from moneta.models import (
    LIQUID_ACCOUNT_TYPES,
    SPEND_ACCOUNT_TYPES,
    Direction,
    RecurringSeries,
)
from moneta.queries import ClassifiedLink, classified_links, linked_txn_ids, primary_currency


class MonthFlows(NamedTuple):
    """One calendar month of `power --history`. Every amount is an unsigned magnitude."""

    month: str  # "YYYY-MM"
    income_cents: int  # inflows tagged to a non-discretionary inflow series
    other_inflows_cents: int  # every other counted inflow: refunds, P2P, one-off deposits
    spend_cents: int
```

In `_accrual`'s docstring, replace `accrual_spend/accrual_income` with
`accrual_spend/accrual_inflows`.

Replace `accrual_income` with:

```python
async def accrual_inflows(
    session: AsyncSession,
    start: date,
    end: date,
    links: list[ClassifiedLink] | None = None,
    primary: str | None = None,
) -> int:
    """Every counted inflow, income or not: history's income plus its other inflows."""
    return await _accrual(session, start, end, links, primary, inflows=True)
```

In `accrual_by_month`:
- Change the return annotation from `-> list[tuple[str, int, int]]:` to
  `-> list[MonthFlows]:`.
- Replace its docstring with:

  ```python
      """Flows per calendar month, newest first (index 0 = today's own, still-in-progress
      month), for the trailing `months` months. Income is what `power` counts as income,
      observed: inflows tagged to a non-discretionary inflow series, ended series
      included, so a past paycheck counts in its own month. Every other inflow is
      reported beside it, never dropped. One SELECT over the whole
      [oldest month start, newest month end] range instead of accrual_inflows/
      accrual_spend called once per month; same exclusions (transfer-linked txns,
      non-spend accounts, non-primary currency), linked_txn_ids computed once."""
  ```

- Directly after the `if primary is None: primary = await primary_currency(session)`
  block, add:

  ```python
      income_series = {
          s.id
          for s in await session.scalars(select(RecurringSeries))
          if s.direction == Direction.inflow and not s.discretionary
      }
  ```

- Replace the bucketing tail, from `income_by_month = dict.fromkeys(...)` to the
  `return`:

  ```python
      income_by_month = dict.fromkeys(label_by_ym.values(), 0)
      spend_by_month = dict.fromkeys(label_by_ym.values(), 0)
      for t in txns:
          if t.id in excluded:
              continue
          label = label_by_ym.get((t.posted_on.year, t.posted_on.month))
          if label is None:
              continue
          if t.amount_cents > 0:
              income_by_month[label] += t.amount_cents
          elif t.amount_cents < 0:
              spend_by_month[label] += -t.amount_cents

      return [(label, income_by_month[label], spend_by_month[label]) for label, _, _ in bounds]
  ```

  with

  ```python
      income_by_month = dict.fromkeys(label_by_ym.values(), 0)
      other_by_month = dict.fromkeys(label_by_ym.values(), 0)
      spend_by_month = dict.fromkeys(label_by_ym.values(), 0)
      for t in txns:
          if t.id in excluded:
              continue
          label = label_by_ym.get((t.posted_on.year, t.posted_on.month))
          if label is None:
              continue
          if t.amount_cents > 0 and t.series_id in income_series:
              income_by_month[label] += t.amount_cents
          elif t.amount_cents > 0:
              other_by_month[label] += t.amount_cents
          elif t.amount_cents < 0:
              spend_by_month[label] += -t.amount_cents

      return [
          MonthFlows(label, income_by_month[label], other_by_month[label], spend_by_month[label])
          for label, _, _ in bounds
      ]
  ```

In `src/moneta/api.py`, replace `PowerMonth` with:

```python
class PowerMonth(BaseModel):
    month: str  # "2026-07"
    income_cents: int  # magnitude: inflows tagged to a non-discretionary inflow series
    other_inflows_cents: int  # magnitude: every other counted inflow
    spend_cents: int  # magnitude
    net_cents: int  # signed: income - spend
```

Replace the body of `power_history`, from its docstring to its `return`, with:

```python
        """Month-over-month income/other inflows/spend, newest first (the newest row is
        the current, still-in-progress month). Unlike GET /power, which reports today's
        recurring-series state, past months here report actual *observed* flows for that
        calendar month, so a since-cancelled bill or a past raise still shows correctly.
        Income is the money power counts as income (inflows tagged to a non-discretionary
        inflow series, ended ones included); every other inflow is other_inflows_cents,
        and net is income - spend."""
        today = date.today()
        links = await classified_links(session)
        primary = await primary_currency(session)
        rows = await accrual_by_month(session, months, today, links=links, primary=primary)
        return [
            PowerMonth(
                month=row.month,
                income_cents=row.income_cents,
                other_inflows_cents=row.other_inflows_cents,
                spend_cents=row.spend_cents,
                net_cents=row.income_cents - row.spend_cents,
            )
            for row in rows
        ]
```

In `src/moneta/cli/main.py` `power`, replace the `--history` option

```python
        typer.Option(
            "--history", help="Show the last N months' income/spend/net instead of this month."
        ),
```

with

```python
        typer.Option(
            "--history",
            help="Show the last N months' income/other inflows/spend/net instead of this month.",
        ),
```

Replace the history table

```python
        table = Table("Month", "Income", "Spend", "Net")
        for m in rows:
            table.add_row(
                m["month"],
                fmt_money(m["income_cents"]),
                fmt_outflow(m["spend_cents"]),
```

with

```python
        table = Table("Month", "Income", "Other inflows", "Spend", "Net")
        for m in rows:
            table.add_row(
                m["month"],
                fmt_money(m["income_cents"]),
                fmt_money(m["other_inflows_cents"]),
                fmt_outflow(m["spend_cents"]),
```

The `net_cents` cell below it is unchanged.

Confirm nothing else uses the old name. `grep -rn accrual_income src tests` must print
nothing.

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_cashflow.py tests/test_overlap_accounting.py tests/test_overlap_preservation.py tests/test_api.py tests/test_cli.py`
Expected: all pass.

- [ ] **Step 5: Update the docs.**

In `docs/PRD.md` §6.3, append this sentence to the end of the **Spending power** row's
description, before the closing ` |`:

```
 `--history N` reports observed months instead: income is inflows tagged to a non-discretionary inflow series (ended ones included), other inflows (refunds, P2P, one-off deposits) are shown beside it, and net = income − spend.
```

In `docs/PRD.md` §7, insert directly under the header separator, above the Task 3 row:

```
| 2026-10-14 | **History income is detected income** | `power --history` income counts only inflows tagged to a non-discretionary inflow series — the definition `power` uses for this month, ended series included — instead of every unlinked inflow, so a friend's repayment, a card refund or a one-off deposit no longer reads as pay (#19). Every other counted inflow is reported beside it as `PowerMonth.other_inflows_cents` (CLI column "Other inflows"); net stays income − spend. `accrual_income` is renamed `accrual_inflows`. Netting card refunds against spend and pairing pass-through money remain open on #19. |
```

In `docs/user-guide.md` **`moneta power` — spending power**, replace the paragraph

```
`moneta power --history N` (e.g. `--history 6`, 1-60 months) replaces the table above with a
compact Month/Income/Spend/Net history instead. The newest row is the current, still-in-progress
month; every row (including that one) reports *actual observed* income and spend for that
calendar month — not today's recurring-series state — so a since-cancelled bill or an old raise
still shows correctly even though no series reflects it anymore.
```

with

```
`moneta power --history N` (e.g. `--history 6`, 1-60 months) replaces the table above with a
compact Month/Income/Other inflows/Spend/Net history instead. The newest row is the current,
still-in-progress month; every row (including that one) reports *actual observed* money for that
calendar month — not today's recurring-series state — so a since-cancelled bill or an old raise
still shows correctly even though no series reflects it anymore. **Income** counts what `power`
counts as income: inflows tagged to a recurring inflow series that isn't a habit (a paycheck),
including a series that has since ended. **Other inflows** is every other inflow that isn't a
transfer — refunds, money from friends, one-off deposits — shown so nothing disappears, but not
income. **Net** = income − spend; other inflows are left out of it.
```

In the user guide's per-field sign table (server-mode section), replace
`` `PowerMonth.income_cents`, `PowerMonth.spend_cents`. `` with:

```
`PowerMonth.income_cents`, `PowerMonth.other_inflows_cents`, `PowerMonth.spend_cents`.
```

In `README.md` (features list, **Spending power** bullet), replace

```
  far" honest when purchases go on credit. `--history N` shows a month-over-month
  income/spend/net table instead.
```

with

```
  far" honest when purchases go on credit. `--history N` shows a month-over-month
  income/other inflows/spend/net table instead, where income is detected income only.
```

In the `README.md` command table's `moneta power [--history N]` row, replace
`` `--history N` shows a month-over-month income/spend/net table instead `` with:

```
`--history N` shows a month-over-month income/other inflows/spend/net table instead
```

`CLAUDE.md` has no conventions paragraph on history income, so nothing there
contradicts this change and `CLAUDE.md` is unchanged in this task.

- [ ] **Step 6: Run the full gate.**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`
Expected: all pass, pristine output.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/views/cashflow.py src/moneta/api.py src/moneta/cli/main.py tests/test_cashflow.py tests/test_api.py tests/test_cli.py tests/test_overlap_accounting.py tests/test_overlap_preservation.py docs/PRD.md docs/user-guide.md README.md
git commit -s -m "fix: power history income is detected income; other inflows shown beside it" -m "Refs #19."
```
