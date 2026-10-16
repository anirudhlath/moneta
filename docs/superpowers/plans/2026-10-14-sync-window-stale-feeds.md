# Resync Window and Stale-Feed Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A re-sync never skips an account's late-arriving, back-dated
transactions. Any account whose feed has gone quiet gets a coverage note, even
when a fresh account at the same source hides it from the global freshness
clock.

**Architecture:**
- `pipelines/run.py::_sync_since` resumes each source from the newest stored
  transaction of its *most-behind active account*, not from the source's newest
  transaction. "Active" is measured against the source's own newest
  transaction.
- A new pure-read helper `queries.stale_accounts(session, logical, today)`
  judges every logical account against its own usual posting rate.
- `GET /power` and `GET /status` carry the result as `stale_accounts`. The
  CLI's `power` and `status` print the server-composed note.

**Tech Stack:** Python 3.13, FastAPI, typer + rich, SQLAlchemy async + SQLite,
pytest (asyncio auto mode), uv.

**Spec:** GitHub issue #41 (the stale-feed part), plus the **Background**
section of this plan. The resync data-loss defect was found in QA; no issue
was filed for it.

## Global Constraints

- Constants, with exact names and values:
  - `pipelines/run.py`: `RESYNC_OVERLAP_DAYS = 7` (unchanged), plus the new
    `ACTIVE_ACCOUNT_DAYS = 90`.
  - `queries.py`: `COVERAGE_WINDOW_DAYS = 365`, `STALE_MIN_DAYS = 7`,
    `STALE_EXPECTED_POSTING_DAYS = 5`.
- Sync window. These stay as they are:
  - first sync and `sync --full` request from the epoch (`1970-01-01`);
  - a source with no accounts yet falls back to the *global* newest txn;
  - physical observations count, including suppressed duplicates
    (`duplicate_of_id IS NOT NULL`).

  The change: `since` = min over the source's active accounts of (account
  newest `posted_on`), minus 7 days. An account is active when its newest is
  `>=` (the source's newest `posted_on` − 90 days). An account with no
  transactions never moves `since`.
- Stale rule. Flag when `days_since >= 7` **and**
  `posting_days * days_since >= 5 * 365`, in integer arithmetic. The terms:
  - `days_since = (today - newest).days`;
  - `posting_days` = distinct `posted_on` dates in the half-open window
    `(today - 365 days, today]`.
- The stale judgment runs on **logical** accounts (`logical.account_roots`):
  - newest = the newest *physical* observation in the logical account,
    suppressed duplicates included;
  - `posting_days` counts **survivors only** (`LogicalData.transactions`);
  - accounts with no survivor in the window are skipped.

  `Account` has no hidden, closed or excluded flag, so every logical account is
  judged.
- Note text, exactly. Here `{label}` is `"{name} ··{mask}"`, or `name` alone
  when `mask` is `None`. The `··` is two U+00B7 characters.

  `{label} has no transactions since {YYYY-MM-DD} (usually posts {rate}) — recent spending on it may be missing.`

  `{rate}` is one of:
  - `~N day(s) a week`, when `round(posting_days * 7 / 365) >= 1`;
  - otherwise `~N day(s) a month`, with
    `N = max(1, round(posting_days * 30 / 365))`.

  The CLI prints `Coverage: {note}` in yellow, with the note passed through
  `escape()`.
- Surfaces: `GET /status`, `GET /power`, and the CLI's `moneta power` and
  `moneta status` (plus their `--json`, which falls out for free). Nothing
  else: not `networth`, `/power/history` or `digest`.
- Views don't commit. The CLI holds zero business logic: the server composes
  `note`, and the CLI only prints it, reading it with `.get(..., [])` so an
  older remote server still works.
- Dates are local-timezone calendar days. The helpers take `today` from the
  caller, and endpoints pass `date.today()`. Never derive a date from UTC.
- No schema change and no migration.
- Test data must be date-relative wherever an endpoint reads `date.today()`
  (API and CLI tests). Pure helpers get an explicit `today`.
- The repo is **public**. Use invented names only (`Acme Card`, `Acme Savings`,
  `Elmton`) and round amounts. No real institutions, merchants, people or
  account suffixes go in code, tests, docs or commit messages.
- Commit with `git commit -s` and no AI co-author trailers.
- Gate, required before every commit:
  `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  All four must pass with pristine output; a warning is a failure. During
  red/green steps, run only the narrow test file.
- Work in `~/code/.worktrees/moneta/fix-sync-window` (branch `fix-sync-window`).
  Two defects, one PR.

## Review Focus

1. **A whole source stalled for months.** Every account at the source is far
   behind today. The late-batch account must still set the window, so the
   outage heals without skipping its rows. This is why dormancy is measured
   from the source's newest txn and not from today. Pinned in Task 1
   (`test_dormancy_is_judged_from_the_sources_own_newest_txn`).
2. **An account with no transactions at all.** Examples: a holdings-only
   brokerage account, or one linked a moment ago. It must not pull the window
   back to the epoch on every sync, and it must not be listed as stale. Pinned
   in Task 1 (`test_account_without_transactions_does_not_reset_the_window`)
   and Task 2 (`test_brand_new_and_empty_accounts_are_not_stale`).
3. **One purchase seen by both providers**, on the same day or up to four days
   apart. It is one posting day: counting both would double the rate and raise
   false notes. A fresh observation that overlap suppressed as a duplicate must
   still keep the account fresh. Pinned in Task 2
   (`test_cross_provider_duplicates_count_one_posting_day`,
   `test_suppressed_duplicate_still_counts_as_the_newest_observation`).
4. **An account whose only transactions are older than 365 days, or that has
   one transaction ever.** It has no usual rate for a silence to contradict, so
   it is never flagged. Pinned in Task 2
   (`test_txns_only_older_than_the_window_never_flag`,
   `test_single_txn_account_never_flags`).
5. **Remote mode against an older server** that sends no `stale_accounts`
   key. `moneta power` must still print normally. Pinned in Task 3
   (`test_power_tolerates_a_server_without_coverage_notes`).

## Background

Every root-cause claim below was checked against the code at `origin/main`
(`eed9005`). The line numbers are current.

### Defect 1: back-dated rows fall outside the resync window (found in QA, no issue filed)

**Symptom:** a source can carry an account whose rows arrive in late batches
next to a sibling account that posts daily. For example, a card feed delivers
a whole month's rows at statement time, back-dated 3–4 weeks. On the next
re-sync, the late account's back-dated batch falls before `since`. Those rows
are never fetched; only `sync --full` recovers them.

**Root cause:** `_sync_since(session, full, source)`
(`src/moneta/pipelines/run.py:27-58`) takes one
`func.max(Transaction.posted_on)` across *all* of the source's accounts (lines
36-43) and subtracts `RESYNC_OVERLAP_DAYS` (7). So `since` = yesterday − 7 as
soon as any account at the source posted yesterday.

**Owner's decision:** `since` = the oldest of (account newest `posted_on` −
7 days), over the source's *active* accounts. An account is active when its
newest txn is within ~90 days, so dormant or closed accounts don't drag the
window back to the epoch. These stay as they are:
- the epoch for first sync and `--full`;
- the no-accounts fallback;
- counting physical observations, including suppressed duplicates.

The SimpleFIN adapter walks ≤45-day windows backward, so a `since` 5–6 weeks
back costs little. Plaid ignores `since`.

**Plan's reading of "within ~90 days":** measured back from the source's own
newest stored txn. On a source that synced recently, that point is today.
During a whole-source outage, the most-behind account still counts (Review
Focus 1). `_sync_since` keeps its signature and needs no `today`.

### Defect 2: one stalled account hides behind a fresh one (#41)

**Symptom:** one account's feed stalls, or the account only posts monthly.
Nothing says so as long as any other account synced. Its recent spending is
silently missing from `spent_so_far`.

**Root cause:** freshness is global. It comes in two forms.
- `queries.latest_successful_sync_at` (`src/moneta/queries.py:101-109`) is the
  newest successful `SyncRun.finished_at`. It feeds:
  - `PowerReport.data_as_of` (`src/moneta/views/power.py:52`, set at `:187`);
  - `NetWorthReport.data_as_of` (`src/moneta/views/networth.py:63`);
  - the CLI's 24-hour `_staleness_footer` (`src/moneta/cli/main.py:53-65`).
- `GET /status` (`src/moneta/api.py:293-318`) reports only the newest run's
  `last_sync_at`/`last_sync_ok`.

No code path looks at any account's newest transaction.

**Owner's decision:** a per-account `stale_accounts` list. Flag an account
when both hold:
- its newest txn is ≥ 7 days old;
- (distinct posting days in the last 365 ÷ 365) × days since its newest txn
  ≥ 5. In other words, at its usual rate we'd have expected ≥ 5 posting days
  by now.

The judgment runs on logical accounts (cross-provider duplicates merged), so
an account fresh via either provider is not stale. Accounts with no history in
the last 365 days are skipped. The wording is a coverage note, not an error.
It is exposed in `/status`, the power response and the CLI footer that prints
freshness, and nowhere else.

How the rule plays out, assuming an account that posted steadily and then went
silent:
- a card used daily is noted after 7 days;
- a card used about 4 days a week is noted after about 9 days;
- a weekly account is noted after about 6 weeks;
- an account posting fewer than about 20 days a year, such as a monthly
  savings account, is never noted.

**Known limitation (documented in Task 3):** with no closed-account flag, a
closed daily-use card keeps its note for up to a year after its last
transaction.

---

## File map

| File | Change |
|---|---|
| `src/moneta/pipelines/run.py` | `ACTIVE_ACCOUNT_DAYS`; `_sync_since` resumes from the most-behind active account (Task 1) |
| `tests/test_run.py` | window tests (Task 1) |
| `src/moneta/queries.py` | stale-feed constants, `StaleAccount`, `feed_is_stale`, `_posting_rate_phrase`, `stale_accounts` (Task 2) |
| `tests/test_queries.py` | coverage-rule tests (Task 2) |
| `src/moneta/views/power.py` | `PowerReport.stale_accounts` (Task 3) |
| `src/moneta/api.py` | `StatusOut.stale_accounts` (Task 3) |
| `src/moneta/cli/main.py` | `_print_coverage`, called by `power` and `status` (Task 3) |
| `tests/test_power.py`, `tests/test_api.py`, `tests/test_cli.py` | surface tests (Task 3) |
| `CLAUDE.md`, `docs/PRD.md`, `docs/user-guide.md`, `README.md` | docs upkeep, inside the task that changes the behavior |

---

### Task 1: Resume each source from its most-behind active account

**Files:**
- Modify: `src/moneta/pipelines/run.py:19-58` (constants block and `_sync_since`)
- Test: `tests/test_run.py`
- Docs: `CLAUDE.md` (the "Sync window is per-source" bullet), `docs/PRD.md`
  (§6.1 rows "Per-source sync windows" and "Incremental sync with overlap",
  plus a §7 history row), `docs/user-guide.md` (Syncing section and one
  troubleshooting row). `README.md` is unchanged: its `--full` advice is still
  true.

**Interfaces:**
- Consumes: nothing new.
- Produces: `moneta.pipelines.run.ACTIVE_ACCOUNT_DAYS: int = 90`.
  `_sync_since(session: AsyncSession, full: bool, source: str) -> date` keeps
  its signature, and `_fetch_all` and `run_sync` are untouched.

- [ ] **Step 1: Write the failing tests.** In `tests/test_run.py`:
  - change the import line
    `from moneta.pipelines.run import RESYNC_OVERLAP_DAYS, _sync_since, run_sync`
    to
    `from moneta.pipelines.run import ACTIVE_ACCOUNT_DAYS, RESYNC_OVERLAP_DAYS, _sync_since, run_sync`;
  - add `AccountType` to the `from moneta.models import (...)` block;
  - append the following after
    `test_sync_since_falls_back_to_global_max_when_source_has_no_accounts_yet`:

```python
async def test_resync_reaches_back_to_the_most_behind_active_account(
    session: AsyncSession,
) -> None:
    """A card that delivers a month of back-dated rows at statement time must not
    lose them behind a sibling account at the same source that posted yesterday."""
    today = date(2026, 7, 9)
    daily = await make_account(session, source="simplefin")
    await make_txn(session, daily, posted_on=today - timedelta(days=1))
    batched = await make_account(session, source="simplefin")
    await make_txn(session, batched, posted_on=today - timedelta(days=33))
    simplefin = RecordingAdapter(source="simplefin")
    await run_sync(session, [simplefin], llm=None, today=today)
    assert simplefin.since == today - timedelta(days=33 + RESYNC_OVERLAP_DAYS)


async def test_dormant_account_does_not_hold_the_window_back(session: AsyncSession) -> None:
    newest = date(2026, 7, 8)
    active = await make_account(session, source="simplefin")
    await make_txn(session, active, posted_on=newest)
    closed = await make_account(session, source="simplefin")
    await make_txn(session, closed, posted_on=newest - timedelta(days=ACTIVE_ACCOUNT_DAYS + 1))
    since = await _sync_since(session, full=False, source="simplefin")
    assert since == newest - timedelta(days=RESYNC_OVERLAP_DAYS)


async def test_account_exactly_at_the_dormancy_horizon_still_counts(
    session: AsyncSession,
) -> None:
    newest = date(2026, 7, 8)
    active = await make_account(session, source="simplefin")
    await make_txn(session, active, posted_on=newest)
    quiet = await make_account(session, source="simplefin")
    await make_txn(session, quiet, posted_on=newest - timedelta(days=ACTIVE_ACCOUNT_DAYS))
    since = await _sync_since(session, full=False, source="simplefin")
    assert since == newest - timedelta(days=ACTIVE_ACCOUNT_DAYS + RESYNC_OVERLAP_DAYS)


async def test_dormancy_is_judged_from_the_sources_own_newest_txn(
    session: AsyncSession,
) -> None:
    """A whole source stalled for months: every account is far behind today, but the
    late-batch account is only weeks behind its sibling, so it still sets the window
    and the outage heals without skipping its rows."""
    daily = await make_account(session, source="simplefin")
    await make_txn(session, daily, posted_on=date(2026, 3, 1))
    batched = await make_account(session, source="simplefin")
    await make_txn(session, batched, posted_on=date(2026, 2, 1))
    simplefin = RecordingAdapter(source="simplefin")
    await run_sync(session, [simplefin], llm=None, today=date(2026, 7, 9))
    assert simplefin.since == date(2026, 2, 1) - timedelta(days=RESYNC_OVERLAP_DAYS)


async def test_suppressed_duplicate_sets_its_accounts_newest(session: AsyncSession) -> None:
    """Each account's newest physical observation counts, including a row suppressed
    as another provider's duplicate."""
    plaid_root = await make_account(session, source="plaid")
    sf_alias = await make_account(session, source="simplefin", canonical_account_id=plaid_root.id)
    sf_daily = await make_account(session, source="simplefin")
    survivor = await make_txn(session, plaid_root, posted_on=date(2026, 6, 6))
    await make_txn(session, sf_alias, posted_on=date(2026, 6, 6), duplicate_of_id=survivor.id)
    await make_txn(session, sf_daily, posted_on=date(2026, 7, 8))
    since = await _sync_since(session, full=False, source="simplefin")
    assert since == date(2026, 6, 6) - timedelta(days=RESYNC_OVERLAP_DAYS)


async def test_account_without_transactions_does_not_reset_the_window(
    session: AsyncSession,
) -> None:
    """A holdings-only brokerage account (or one linked moments ago) has no newest
    txn; it must not drag the window back to the epoch on every sync."""
    checking = await make_account(session, source="simplefin")
    await make_txn(session, checking, posted_on=date(2026, 7, 5))
    await make_account(session, source="simplefin", type=AccountType.brokerage)
    since = await _sync_since(session, full=False, source="simplefin")
    assert since == date(2026, 7, 5) - timedelta(days=RESYNC_OVERLAP_DAYS)
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  Run `uv run pytest tests/test_run.py -q -p no:randomly`.
  Expected: a collection error,
  `ImportError: cannot import name 'ACTIVE_ACCOUNT_DAYS' from 'moneta.pipelines.run'`.

- [ ] **Step 3: Add only the constant, to see the behavioral red.** In
  `src/moneta/pipelines/run.py`, directly under `RESYNC_OVERLAP_DAYS = 7`, add:

```python
# An account whose newest txn sits more than this many days behind its source's
# newest txn is dormant (closed, paid off): it no longer holds the window back.
ACTIVE_ACCOUNT_DAYS = 90
```

  Run `uv run pytest tests/test_run.py -q -p no:randomly`. Expected: exactly
  4 failures, each a `since` that is too recent (the source's newest − 7):
  - `test_resync_reaches_back_to_the_most_behind_active_account` (got 2026-07-01);
  - `test_account_exactly_at_the_dormancy_horizon_still_counts`;
  - `test_dormancy_is_judged_from_the_sources_own_newest_txn` (got 2026-02-22);
  - `test_suppressed_duplicate_sets_its_accounts_newest` (got 2026-07-01).

  `test_dormant_account_does_not_hold_the_window_back` and
  `test_account_without_transactions_does_not_reset_the_window` already pass.
  They guard against overcorrecting toward the epoch.

- [ ] **Step 4: Implement.** In `src/moneta/pipelines/run.py`, replace the
  whole `_sync_since` function (lines 27-58) with:

```python
async def _sync_since(session: AsyncSession, full: bool, source: str) -> date:
    if full:
        return _EPOCH
    has_source = (
        await session.execute(
            select(func.count()).select_from(Account).where(Account.source == source)
        )
    ).scalar_one()
    newest: date | None
    if has_source:
        # Resume from this source's most-behind ACTIVE account, never from the source's
        # newest txn: a card that delivers a month of back-dated rows at statement time
        # must not lose them behind a sibling account that posted yesterday. Each
        # account's newest physical observation counts, suppressed duplicates included;
        # an account with no transactions has no row here and never moves the window.
        per_account = list(
            await session.scalars(
                select(func.max(Transaction.posted_on))
                .join(Account, Transaction.account_id == Account.id)
                .where(Account.source == source)
                .group_by(Transaction.account_id)
            )
        )
        # dormancy is measured from the source's own newest txn, not today, so a whole
        # source stalled for months still resumes from its most-behind account
        horizon = max(per_account, default=_EPOCH) - timedelta(days=ACTIVE_ACCOUNT_DAYS)
        newest = min((d for d in per_account if d >= horizon), default=None)
    else:
        # no accounts carry this source yet (first run after upgrading onto
        # source-attributed accounts) — fall back to the global max so a
        # known-but-not-yet-backfilled source doesn't re-pull full history.
        # NOTE: this also fires for a genuinely new source added to an already-
        # populated DB (its first-ever sync) — it inherits the global newest date
        # instead of the epoch, truncating its first pull; deliberate/accepted,
        # use `sync --full` when adding a new source.
        newest = (
            await session.execute(select(func.max(Transaction.posted_on)))
        ).scalar_one_or_none()
    if newest is None:
        return _EPOCH
    return newest - timedelta(days=RESYNC_OVERLAP_DAYS)
```

- [ ] **Step 5: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_run.py tests/test_api.py tests/test_overlap_accounting.py -q -p no:randomly`.
  Expected: PASS. `test_api.py` and `test_overlap_accounting.py` pin
  single-active-account windows that must not move.

- [ ] **Step 6: Update the docs.** Make these exact replacements.

  `CLAUDE.md`, in the "Sync window is per-source, not global" bullet. Replace

  > `_sync_since(session, full, source)` computes each adapter's `since` independently from physical observations, including suppressed duplicates, using the newest `posted_on` of transactions whose `Account.source == source` (`Account.source`, migration `0004`, stamped by every `AccountDTO`/adapter). First sync and `sync --full` request from the epoch (1970-01-01) for every source — there is deliberately no window constant; a normal re-sync resumes each source from *that source's own* newest stored txn minus a 7-day overlap (`RESYNC_OVERLAP_DAYS`, `pipelines/run.py`).

  with

  > `_sync_since(session, full, source)` computes each adapter's `since` independently from physical observations, including suppressed duplicates, of the accounts whose `Account.source == source` (`Account.source`, migration `0004`, stamped by every `AccountDTO`/adapter). First sync and `sync --full` request from the epoch (1970-01-01) for every source — there is deliberately no window constant; a normal re-sync resumes each source from the newest stored txn of its *most-behind active account* minus a 7-day overlap (`RESYNC_OVERLAP_DAYS`, `pipelines/run.py`) — never from the source's newest txn, because one account can deliver a month of back-dated rows at statement time while a sibling posts daily, and a window cut from the sibling would skip that batch for good. An account is active while its newest txn is within 90 days (`ACTIVE_ACCOUNT_DAYS`) of the *source's* newest txn — measured from the source, not today, so a whole source stalled for months still resumes from its most-behind account; dormant (closed, paid-off) accounts and accounts with no transactions never hold the window back.

  `docs/PRD.md` §6.1, row "Per-source sync windows". Replace
  `each source's incremental window is computed from *that source's own* newest stored transaction (`
  with
  `each source's incremental window is computed from *that source's own* accounts' stored transactions (`.

  `docs/PRD.md` §6.1, row "Incremental sync with overlap". Replace the
  description cell's first sentence,
  `Each source resumes from *its own* newest stored transaction minus a 7-day overlap;`
  with
  `Each source resumes from the newest stored transaction of *its own* most-behind active account (newest transaction within 90 days of the source's newest) minus a 7-day overlap, so an account whose rows arrive weeks late in one batch never loses them behind a sibling account that posted yesterday, and a closed account doesn't hold the window back;`.

  `docs/PRD.md` §7. Insert as the first data row, directly under
  `|---|---|---|`:

```markdown
| 2026-10-14 | **Resync window covers late-batch accounts** | `_sync_since` resumes each source from the newest transaction of its most-behind active account (newest within 90 days of the source's newest, `ACTIVE_ACCOUNT_DAYS`) minus the 7-day overlap, instead of from the source's newest transaction. A card that delivers a month of back-dated rows at statement time no longer loses them behind a sibling account that posted yesterday — before, only `sync --full` recovered them. Dormant accounts and accounts with no transactions never pull the window back. |
```

  `docs/user-guide.md`, Syncing code block. Replace
  `moneta sync            # incremental: resumes from your newest transaction (with overlap)`
  with
  `moneta sync            # incremental: resumes where each source's accounts left off (with overlap)`.

  `docs/user-guide.md`, Syncing section. Replace

  > Each configured source (SimpleFIN, Plaid, ...) syncs on its **own** incremental window,
  > computed from that source's own newest stored transaction, including observations
  > superseded by another provider — not a single global window. This

  with

  > Each configured source (SimpleFIN, Plaid, ...) syncs on its **own** incremental window —
  > not a single global window. The window starts a week before the newest stored transaction
  > of the source's most-behind active account (observations superseded by another provider
  > count), so a card that delivers a month of back-dated transactions at statement time gets
  > them on the next sync even when another account at the same source posted yesterday. An
  > account counts as active while its newest transaction is within 90 days of the source's
  > newest; a closed account stops holding the window back. This

  `docs/user-guide.md`, Troubleshooting. Replace the "A bank's history is
  missing" row with:

```markdown
| A bank's history is missing | `moneta sync --full` re-pulls everything the institution retains for every configured source. It's needed after adding a new source or linking a new account at an existing one, and after an account was quiet for more than 90 days while others at its source kept posting. An outage, or a card whose transactions arrive in late batches, heals on the next plain `moneta sync`. |
```

- [ ] **Step 7: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/run.py tests/test_run.py CLAUDE.md docs/PRD.md docs/user-guide.md
git commit -s -m "fix: resync from each source's most-behind active account, not its newest txn"
```

---

### Task 2: Judge each logical account's feed against its own posting rate

**Files:**
- Modify: `src/moneta/queries.py`: imports (lines 13 and 21-30), plus new code
  directly after `latest_successful_sync_at` (line 109)
- Test: `tests/test_queries.py`
- Docs: none in this task. Nothing user-visible changes until Task 3 surfaces
  the result.

**Interfaces:**
- Consumes: `moneta.logical.LogicalData` (`accounts`, `account_roots`,
  `transactions` = survivors only).
- Produces, all in `moneta.queries`:
  - `COVERAGE_WINDOW_DAYS = 365`, `STALE_MIN_DAYS = 7`,
    `STALE_EXPECTED_POSTING_DAYS = 5`;
  - `class StaleAccount(BaseModel)` with fields `account_id: int`, `name: str`,
    `mask: str | None`, `newest_posted_on: date`, `posting_days: int` and
    `note: str`;
  - `feed_is_stale(posting_days: int, days_since: int) -> bool`;
  - `_posting_rate_phrase(posting_days: int) -> str`;
  - `async stale_accounts(session: AsyncSession, logical: LogicalData, today: date) -> list[StaleAccount]`,
    sorted by `account_id`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_queries.py`,
  replace the import header (everything above
  `from tests.factories import make_account, make_txn`) with the block below.
  Write it exactly like this: the gate runs `ruff check` (E501, isort) before
  `ruff format`.

```python
from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.logical import load_logical_data
from moneta.models import Account, AccountType, Cadence, Transaction, TransferLink
from moneta.queries import (
    ClassifiedLink,
    StaleAccount,
    _posting_rate_phrase,
    classified_links,
    feed_is_stale,
    loan_payment_stats,
    stale_accounts,
)
```

  Then append:

```python
_TODAY = date(2026, 7, 9)


async def _post_on(
    session: AsyncSession, account: Account, days_ago: Iterable[int]
) -> list[Transaction]:
    """One transaction per listed day before _TODAY, in the order given."""
    return [
        await make_txn(session, account, posted_on=_TODAY - timedelta(days=d)) for d in days_ago
    ]


def test_feed_is_stale_boundaries() -> None:
    # 25 posting days a year over 73 quiet days is exactly 5 expected posting days
    assert feed_is_stale(posting_days=25, days_since=73)
    assert not feed_is_stale(posting_days=24, days_since=73)
    # even a daily feed gets a full week before it is called quiet
    assert not feed_is_stale(posting_days=365, days_since=6)
    assert feed_is_stale(posting_days=365, days_since=7)
    assert not feed_is_stale(posting_days=0, days_since=365)


def test_posting_rate_phrase() -> None:
    assert _posting_rate_phrase(187) == "~4 days a week"
    assert _posting_rate_phrase(52) == "~1 day a week"
    assert _posting_rate_phrase(24) == "~2 days a month"
    assert _posting_rate_phrase(5) == "~1 day a month"


async def test_daily_card_quiet_for_two_weeks_is_stale(session: AsyncSession) -> None:
    card = await make_account(session, name="Acme Card", mask="1234", type=AccountType.credit)
    await _post_on(session, card, range(14, 201))  # daily, then silent for 14 days
    stale = await stale_accounts(session, await load_logical_data(session), _TODAY)
    assert stale == [
        StaleAccount(
            account_id=card.id,
            name="Acme Card",
            mask="1234",
            newest_posted_on=date(2026, 6, 25),
            posting_days=187,
            note=(
                "Acme Card ··1234 has no transactions since 2026-06-25 "
                "(usually posts ~4 days a week) — recent spending on it may be missing."
            ),
        )
    ]


async def test_monthly_savings_account_is_not_stale(session: AsyncSession) -> None:
    savings = await make_account(session, name="Acme Savings", type=AccountType.savings)
    await _post_on(session, savings, range(40, 400, 30))  # monthly; newest 40 days ago
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_account_fresh_via_either_provider_is_not_stale(session: AsyncSession) -> None:
    root = await make_account(session, source="simplefin", name="Acme Card", mask="1234")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    await _post_on(session, root, range(20, 201))  # this provider stalled 20 days ago
    await _post_on(session, alias, range(1, 20))  # the other one kept posting
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_suppressed_duplicate_still_counts_as_the_newest_observation(
    session: AsyncSession,
) -> None:
    """Overlap kept the older-dated observation of the newest purchase; the other
    provider's row, four days fresher, still proves the feed is alive."""
    root = await make_account(session, source="simplefin", name="Acme Card", mask="1234")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    survivors = await _post_on(session, root, range(9, 221))  # stale on survivors alone
    await make_txn(
        session,
        alias,
        posted_on=_TODAY - timedelta(days=5),
        duplicate_of_id=survivors[0].id,
    )
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_cross_provider_duplicates_count_one_posting_day(session: AsyncSession) -> None:
    """One purchase seen by both providers a day apart is one posting day: counting
    both observations would double this weekly account's rate and flag it."""
    root = await make_account(session, source="simplefin", name="Acme Card", mask="1234")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    for week in range(50):  # weekly, newest 20 days ago
        survivor = await make_txn(session, root, posted_on=_TODAY - timedelta(days=20 + 7 * week))
        await make_txn(
            session,
            alias,
            posted_on=_TODAY - timedelta(days=21 + 7 * week),
            duplicate_of_id=survivor.id,
        )
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_txns_only_older_than_the_window_never_flag(session: AsyncSession) -> None:
    old = await make_account(session, name="Elmton Card")
    await _post_on(session, old, range(366, 500))  # daily, but over a year ago
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_single_txn_account_never_flags(session: AsyncSession) -> None:
    for days_ago in (7, 30, 200, 364):
        account = await make_account(session)
        await make_txn(session, account, posted_on=_TODAY - timedelta(days=days_ago))
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []


async def test_brand_new_and_empty_accounts_are_not_stale(session: AsyncSession) -> None:
    new = await make_account(session)
    await _post_on(session, new, range(0, 4))  # first sync brought only this week
    await make_account(session, type=AccountType.brokerage)  # holdings only, no txns
    assert await stale_accounts(session, await load_logical_data(session), _TODAY) == []
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  Run `uv run pytest tests/test_queries.py -q -p no:randomly`.
  Expected: a collection error,
  `ImportError: cannot import name 'StaleAccount' from 'moneta.queries'`.

- [ ] **Step 3: Implement.** In `src/moneta/queries.py`:
  - change `from datetime import date, datetime` to
    `from datetime import date, datetime, timedelta`;
  - add `Transaction` to the `from moneta.models import (...)` block, on its
    own line between `SyncRun,` and `TransferLink,`.

  Then insert directly after `latest_successful_sync_at`:

```python
# Coverage rule (stale feeds): a logical account is flagged once its newest txn is at
# least STALE_MIN_DAYS old AND its usual rate — distinct posting days over the trailing
# COVERAGE_WINDOW_DAYS — predicts at least STALE_EXPECTED_POSTING_DAYS posting days since.
COVERAGE_WINDOW_DAYS = 365
STALE_MIN_DAYS = 7
STALE_EXPECTED_POSTING_DAYS = 5


class StaleAccount(BaseModel):
    """A logical account whose feed went quiet for longer than its usual posting rate
    explains — a coverage note, not an error. `note` is the full user-facing sentence."""

    account_id: int
    name: str
    mask: str | None
    newest_posted_on: date
    posting_days: int  # distinct posting days in the trailing COVERAGE_WINDOW_DAYS
    note: str


def feed_is_stale(posting_days: int, days_since: int) -> bool:
    """`posting_days / 365 * days_since >= 5`, in integers so the boundary is exact."""
    return (
        days_since >= STALE_MIN_DAYS
        and posting_days * days_since >= STALE_EXPECTED_POSTING_DAYS * COVERAGE_WINDOW_DAYS
    )


def _posting_rate_phrase(posting_days: int) -> str:
    # integer posting_days can never land on a .5 here, so round() has no ties to break
    per_week = round(posting_days * 7 / COVERAGE_WINDOW_DAYS)
    if per_week >= 1:
        return f"~{per_week} day{'' if per_week == 1 else 's'} a week"
    per_month = max(1, round(posting_days * 30 / COVERAGE_WINDOW_DAYS))
    return f"~{per_month} day{'' if per_month == 1 else 's'} a month"


async def stale_accounts(
    session: AsyncSession, logical: LogicalData, today: date
) -> list[StaleAccount]:
    """Logical accounts whose feed looks stalled, each judged on its own history.

    Newest is the newest *physical* observation in the logical account — suppressed
    duplicates included — so an account fresh via either provider is fresh. Posting
    days count survivors only, so one purchase seen by both providers days apart is one
    posting day. Accounts without a transaction in the window are skipped.
    """
    newest: dict[int, date] = {}
    rows = await session.execute(
        select(Transaction.account_id, func.max(Transaction.posted_on)).group_by(
            Transaction.account_id
        )
    )
    for account_id, posted_on in rows.tuples():
        root = logical.account_roots[account_id]
        newest[root] = max(newest.get(root, posted_on), posted_on)
    window_start = today - timedelta(days=COVERAGE_WINDOW_DAYS)
    posting: dict[int, set[date]] = {}
    for txn in logical.transactions:
        if window_start < txn.posted_on <= today:
            posting.setdefault(logical.account_roots[txn.account_id], set()).add(txn.posted_on)
    stale: list[StaleAccount] = []
    for root_id, days in sorted(posting.items()):
        last = newest[root_id]
        if not feed_is_stale(len(days), (today - last).days):
            continue
        account = logical.accounts[root_id]
        label = f"{account.name} ··{account.mask}" if account.mask else account.name
        stale.append(
            StaleAccount(
                account_id=root_id,
                name=account.name,
                mask=account.mask,
                newest_posted_on=last,
                posting_days=len(days),
                note=(
                    f"{label} has no transactions since {last.isoformat()} "
                    f"(usually posts {_posting_rate_phrase(len(days))}) — "
                    "recent spending on it may be missing."
                ),
            )
        )
    return stale
```

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_queries.py -q -p no:randomly`. Expected: PASS.

- [ ] **Step 5: Run the full gate, then commit.** This is an internal helper
  with no caller yet, so docs are deferred to Task 3, which surfaces it.

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/queries.py tests/test_queries.py
git commit -s -m "feat: judge each account's feed against its own posting rate"
```

---

### Task 3: Surface `stale_accounts` in `/power`, `/status` and the CLI

**Files:**
- Modify: `src/moneta/views/power.py`: imports (lines 17-23), `PowerReport`
  (line 52) and the `PowerReport(...)` return (lines 188-201)
- Modify: `src/moneta/api.py`: the `moneta.queries` import (line 49),
  `StatusOut` (lines 153-159) and the `/status` endpoint (lines 293-318)
- Modify: `src/moneta/cli/main.py`: a new `_print_coverage` after
  `_staleness_footer` (line 65), called in `status` (after line 232) and in
  `power` (before line 284)
- Test: `tests/test_power.py`, `tests/test_api.py`, `tests/test_cli.py`
- Docs: `docs/user-guide.md`, `docs/PRD.md`, `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: `moneta.queries.StaleAccount` and
  `moneta.queries.stale_accounts(session, logical, today)` from Task 2.
- Produces:
  - `PowerReport.stale_accounts: list[StaleAccount]`;
  - `StatusOut.stale_accounts: list[StaleAccount]`. JSON shape per entry:
    `{"account_id": int, "name": str, "mask": str | null, "newest_posted_on": "YYYY-MM-DD", "posting_days": int, "note": str}`;
  - `moneta.cli.main._print_coverage(payload: dict[str, Any]) -> None`.

- [ ] **Step 1: Write the failing tests.**

  `tests/test_power.py`: change `from datetime import date, datetime` to
  `from datetime import date, datetime, timedelta`, then append:

```python
async def test_power_reports_quiet_feeds(session: AsyncSession) -> None:
    today = date(2026, 7, 9)
    card = await make_account(session, name="Acme Card", mask="1234", type=AccountType.credit)
    for days_ago in range(14, 201):
        await make_txn(session, card, posted_on=today - timedelta(days=days_ago))
    report = await power_report(session, today=today)
    assert [(s.account_id, s.newest_posted_on) for s in report.stale_accounts] == [
        (card.id, date(2026, 6, 25))
    ]


async def test_power_reports_no_quiet_feeds_on_an_empty_db(session: AsyncSession) -> None:
    report = await power_report(session, today=date(2026, 7, 7))
    assert report.stale_accounts == []
```

  `tests/test_api.py`:
  - add `AccountType,` as the first name in the `from moneta.models import (...)`
    block, above `EventKind,`;
  - in `test_status_endpoint_fresh_db`, add `"stale_accounts": [],` as the last
    key of the expected dict;
  - append:

```python
async def test_status_and_power_list_accounts_whose_feed_went_quiet(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
) -> None:
    async with sessionmaker() as session:
        card = await make_account(session, name="Acme Card", mask="1234", type=AccountType.credit)
        for days_ago in range(14, 201):
            await make_txn(session, card, posted_on=_TODAY - timedelta(days=days_ago))
        await session.commit()
    quiet_since = (_TODAY - timedelta(days=14)).isoformat()
    expected = [
        {
            "account_id": card.id,
            "name": "Acme Card",
            "mask": "1234",
            "newest_posted_on": quiet_since,
            "posting_days": 187,
            "note": (
                f"Acme Card ··1234 has no transactions since {quiet_since} "
                "(usually posts ~4 days a week) — recent spending on it may be missing."
            ),
        }
    ]
    assert (await client.get("/status")).json()["stale_accounts"] == expected
    assert (await client.get("/power")).json()["stale_accounts"] == expected
```

  `tests/test_cli.py`: append:

```python
def _seed_quiet_card(tmp_path: Path) -> str:
    """A daily-use card silent for two weeks; returns the date it went quiet."""
    today = date.today()

    async def seed(session: AsyncSession) -> None:
        card = await make_account(session, name="Acme Card", mask="1234", type=AccountType.credit)
        for days_ago in range(14, 201):
            await make_txn(session, card, posted_on=today - timedelta(days=days_ago))

    _seed_db(tmp_path, seed)
    return (today - timedelta(days=14)).isoformat()


def _coverage_line(quiet_since: str) -> str:
    return (
        f"Coverage: Acme Card ··1234 has no transactions since {quiet_since} "
        "(usually posts ~4 days a week) — recent spending on it may be missing."
    )


def test_power_prints_coverage_note_for_a_quiet_feed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    quiet_since = _seed_quiet_card(tmp_path)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())  # rich wraps long lines at the terminal width
    assert _coverage_line(quiet_since) in flat


def test_status_prints_coverage_note_for_a_quiet_feed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    quiet_since = _seed_quiet_card(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert _coverage_line(quiet_since) in " ".join(result.output.split())


def test_power_tolerates_a_server_without_coverage_notes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Remote mode against an older server: no `stale_accounts` key, no crash."""

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return {
            "month": "2026-07",
            "monthly_income_cents": 0,
            "income_sources": [],
            "fixed_costs": [],
            "total_fixed_cents": 0,
            "spending_power_cents": 0,
            "spent_so_far_cents": 0,
            "remaining_cents": 0,
            "days_left": 23,
            "per_day_remaining_cents": 0,
            "upcoming": [],
            "data_as_of": None,
        }

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    assert "Spending power" in result.output
    assert "Coverage:" not in result.output
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  Run `uv run pytest tests/test_power.py tests/test_api.py tests/test_cli.py -q -p no:randomly`.
  Expected failures:
  - `AttributeError: 'PowerReport' object has no attribute 'stale_accounts'`
    (both `test_power` tests);
  - a dict mismatch in `test_status_endpoint_fresh_db`;
  - `KeyError: 'stale_accounts'` in the new API test;
  - `AssertionError` in both CLI coverage tests.

  `test_power_tolerates_a_server_without_coverage_notes` already passes. It
  guards the `.get` you add next.

- [ ] **Step 3: Implement the view and the endpoint.**

  `src/moneta/views/power.py`: replace the `from moneta.queries import (...)`
  block with the one below. isort puts classes before functions, and
  `ruff check` runs before `ruff format`.

```python
from moneta.queries import (
    StaleAccount,
    classified_links,
    foreign_series_ids,
    latest_successful_sync_at,
    loan_payment_stats,
    primary_currency,
    stale_accounts,
)
```

  Then add this field to `PowerReport`, after `data_as_of`:

```python
    stale_accounts: list[StaleAccount]  # per-account coverage notes (queries.stale_accounts)
```

  In `power_report`, add this as the last keyword of the returned
  `PowerReport(...)`, after `data_as_of=data_as_of,`:

```python
        stale_accounts=await stale_accounts(session, logical, today),
```

  `src/moneta/api.py`: replace line 49
  (`from moneta.queries import bill_amount, classified_links, primary_currency`)
  with:

```python
from moneta.queries import (
    StaleAccount,
    bill_amount,
    classified_links,
    primary_currency,
    stale_accounts,
)
```

  Add this field as the last one of `StatusOut`:

```python
    stale_accounts: list[StaleAccount]
```

  Replace the `/status` endpoint body (lines 293-318) with:

```python
    @app.get("/status")
    async def status(session: Session) -> StatusOut:
        """Overall health: booleans, counts and per-account coverage notes — never
        secrets (design 2026-07-16 §3)."""
        last_run = await _latest_sync_run(session)
        last_sync_at = last_run.finished_at if last_run is not None else None
        # last_sync_ok is unknown (None), not False, while the newest run is still in
        # flight — a bare boolean would misreport an unfinished sync as a failure
        last_sync_ok = (
            last_run.success if last_run is not None and last_run.finished_at is not None else None
        )
        logical = await load_logical_data(session)
        open_reviews_count = (
            await session.execute(
                select(func.count())
                .select_from(ReviewItem)
                .where(ReviewItem.status == ReviewStatus.open)
            )
        ).scalar_one()
        return StatusOut(
            last_sync_at=last_sync_at,
            last_sync_ok=last_sync_ok,
            accounts=len(logical.accounts),
            open_reviews=open_reviews_count,
            aggregators=[a.source for a in adapters],
            llm_configured=llm is not None,
            stale_accounts=await stale_accounts(session, logical, date.today()),
        )
```

- [ ] **Step 4: Implement the CLI.** In `src/moneta/cli/main.py`, insert
  directly after `_staleness_footer`:

```python
def _print_coverage(payload: dict[str, Any]) -> None:
    """One yellow line per account the server judged stale — a coverage note, not an
    error. `.get`: an older server in remote mode sends no `stale_accounts`."""
    for account in payload.get("stale_accounts", []):
        console.print(f"[yellow]Coverage: {escape(account['note'])}[/yellow]")
```

  In `power()`, insert `_print_coverage(r)` on the line directly before
  `if footer := _staleness_footer(r.get("data_as_of")):`. In `status()`,
  append `_print_coverage(st)` as the function's last statement, after the
  `Accounts: … · LLM: …` `console.print(...)`.

- [ ] **Step 5: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_power.py tests/test_api.py tests/test_cli.py tests/test_queries.py -q -p no:randomly`.
  Expected: PASS.

- [ ] **Step 6: Update the docs.** Make these exact edits.

  `docs/user-guide.md`, Contents. After the line
  `  - [Staleness footers](#staleness-footers)`, insert
  `  - [Coverage notes](#coverage-notes)`.

  `docs/user-guide.md`, `moneta status` example. Replace the output block with:

```
Last sync: 2026-07-16T06:00:03 → ok
  12 new txns, 1 new series, 0 events
Accounts: 8  ·  Open review items: 2
Aggregators: simplefin, plaid  ·  LLM: configured
Coverage: Acme Card ··1234 has no transactions since 2026-07-02 (usually posts ~4 days a week) — recent spending on it may be missing.
```

  `docs/user-guide.md`, the paragraph under that example. Replace
  `These are booleans/counts only — never credentials.` with
  `These are booleans/counts only — never credentials. Below them comes one yellow `Coverage:` line for each account whose feed has gone quiet (see [Coverage notes](#coverage-notes)); none when every feed looks alive.`

  `docs/user-guide.md`, the `moneta status --json` paragraph and example. In
  the field list, replace
  `(`last_sync_at`, `last_sync_ok`, `accounts`, `open_reviews`, `aggregators`, `llm_configured`)`
  with
  `(`last_sync_at`, `last_sync_ok`, `accounts`, `open_reviews`, `aggregators`, `llm_configured`, `stale_accounts`)`.
  In the JSON example, replace the line
  ` "open_reviews": 2, "aggregators": ["simplefin", "plaid"], "llm_configured": true,`
  with these two lines:

```
 "open_reviews": 2, "aggregators": ["simplefin", "plaid"], "llm_configured": true,
 "stale_accounts": [],
```

  `docs/user-guide.md`, new subsection, inserted directly after the Staleness
  footers subsection. Its last line is
  `speaks up when the numbers you're looking at might be stale.`, and the new
  subsection goes before `### `moneta digest` — notifications`:

````markdown
### Coverage notes

The staleness footer only knows when the last sync ran. A sync can succeed while one
account's feed has stalled — or while a bank holds that card's transactions back for a
monthly batch — so `moneta power` and `moneta status` also judge every account on its own
and print a yellow line for each one that has gone quiet:

```
Coverage: Acme Card ··1234 has no transactions since 2026-07-02 (usually posts ~4 days a week) — recent spending on it may be missing.
```

An account is noted when its newest transaction is at least 7 days old **and**, at its usual
rate — distinct posting days over the last 365 days — you'd have expected at least 5 posting
days since then. A card you use every day is noted after a week of silence, one you use a few
days a week after about ten days, a weekly account after about six weeks, and an account that
posts once or twice a month never is. An account linked across providers is judged once, and
a transaction from either provider counts. Accounts with no transactions in the last year are
skipped.

It's a note, not an error: nothing is dropped from the numbers, but spending you made on that
account recently may not be in them yet. Run `moneta sync`; if the note stays, fix the
account's connection at its provider, then run `moneta sync --full` to backfill. moneta has no
closed-account flag, so a card you closed can keep its note for up to a year after its last
transaction.

The same list is `stale_accounts` in `GET /power`, `GET /status` and their `--json` output —
`account_id`, `name`, `mask`, `newest_posted_on`, `posting_days` (distinct posting days in the
last 365) and the `note` text.
````

  `docs/user-guide.md`, Scripting `--json` paragraph. Replace
  ``(`last_sync_at`, `last_sync_ok`, `accounts`, `open_reviews`, `aggregators`,
  `llm_configured`)`` (it wraps across two lines there) with
  ``(`last_sync_at`, `last_sync_ok`, `accounts`, `open_reviews`, `aggregators`,
  `llm_configured`, `stale_accounts`)``.

  `docs/user-guide.md`, Troubleshooting. Insert this row directly after the
  `power`/`networth` "data as of" row:

```markdown
| `power`/`status` print a yellow `Coverage:` line | That account's feed has been quiet for longer than its usual posting rate explains, so its recent spending may be missing. Run `moneta sync`; if it persists, fix the account's connection at its provider, then `moneta sync --full`. See [Coverage notes](#coverage-notes). |
```

  `docs/PRD.md` §6.1, row "`/status` + staleness". Append to the end of its
  description cell, before the closing `|`:
  ` Both `GET /status` and `GET /power` also carry `stale_accounts`: per logical account, a coverage note when its newest transaction is ≥7 days old and its trailing-365-day rate of posting days predicts ≥5 posting days since; `moneta power`/`moneta status` print each as a yellow `Coverage:` line, so one stalled account can't hide behind a fresh one.`

  `docs/PRD.md` §7. Insert as the first data row, above the Task 1 row:

```markdown
| 2026-10-14 | **Coverage notes for quiet feeds** | `queries.stale_accounts` judges every logical account on its own history: flagged when its newest transaction (either provider; suppressed duplicates count) is at least 7 days old and its distinct posting days over the last 365 days predict at least 5 posting days since (`STALE_MIN_DAYS`, `STALE_EXPECTED_POSTING_DAYS`, `COVERAGE_WINDOW_DAYS`). `GET /status` and `GET /power` carry the list as `stale_accounts`; `moneta power` and `moneta status` print each server-composed note as a yellow `Coverage:` line. A daily-use card silent for a week is noted; a monthly savings account is not. Freshness was previously only global (`data_as_of`), so one stalled account hid behind any fresh one. |
```

  `docs/PRD.md` §9, "Aggregator data quality" bullet. Replace
  `now surface a stalled or failing source instead of masking it;` with
  `now surface a stalled or failing source instead of masking it, and per-account coverage notes surface one stalled account behind a fresh one;`.

  `README.md`, command table.
  - In the `moneta power [--history N]` row, replace
    `per-day, upcoming charges;` with
    `per-day, upcoming charges, coverage notes for accounts whose feed went quiet;`.
  - In the `moneta status` row, replace `configured sources, LLM status |`
    with `configured sources, LLM status, coverage notes for accounts whose feed went quiet |`.

  `CLAUDE.md`.
  - Insert this bullet directly after the "Sync window is per-source, not
    global" bullet:

  > - **Coverage is per logical account; freshness is global:** `data_as_of` only says when the newest successful sync ran. `queries.stale_accounts(session, logical, today)` — surfaced as `stale_accounts` in `GET /power` and `GET /status`, and as yellow `Coverage:` lines in `moneta power`/`moneta status` — flags each logical account whose newest txn is ≥ `STALE_MIN_DAYS` (7) old and whose distinct posting days over the trailing `COVERAGE_WINDOW_DAYS` (365) predict ≥ `STALE_EXPECTED_POSTING_DAYS` (5) posting days since (`feed_is_stale`, integer math). Newest is the newest *physical* observation (suppressed duplicates included, so either provider keeps an account fresh); posting days count *survivors* only (one purchase seen by both providers days apart is one day). The server composes `note`; the CLI only prints it, via `.get` so an older remote server still works. There is no closed-account flag, so a closed card stays noted for up to a year.

  - In Layout, replace
    `` - `queries.py` — shared cross-table lookups (`classified_links`, `account_type_map`, `primary_currency`) ``
    with
    `` - `queries.py` — shared cross-table lookups (`classified_links`, `account_type_map`, `primary_currency`, `stale_accounts`) ``.

- [ ] **Step 7: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/views/power.py src/moneta/api.py src/moneta/cli/main.py \
  tests/test_power.py tests/test_api.py tests/test_cli.py \
  CLAUDE.md docs/PRD.md docs/user-guide.md README.md
git commit -s -m "feat: coverage notes for accounts whose feed went quiet in power and status"
```

---

### Task 4: Whole-branch check and PR

**Files:** none changed, unless the checks below find something.

- [ ] **Step 1: Run the full gate on the branch tip.**
  Run `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  Expected: all four pristine, and `git status` clean afterwards (format
  changed nothing).

- [ ] **Step 2: Scan the branch for private data.** Run
  `git diff origin/main -U0 | grep -nE '\$[0-9]+|[0-9]{4,}'`. Every hit must
  be one of these:
  - an invented example: the `1234` mask, the 2026 dates, or the
    `1970-01-01` epoch;
  - code: constants, line numbers, test day counts.

  No real institution, merchant, person or account suffix may appear anywhere
  in the diff.

- [ ] **Step 3: Hand off.** Use superpowers:finishing-a-development-branch. If
  the owner picks a PR, use:
  - Title: `fix: resync window covers late-batch accounts; coverage notes for quiet feeds`
  - Body: two short sections, one per defect. Each section gives the symptom,
    the root cause and the fix, in this plan's Background wording. End the
    body with `Refs #41 (the stale-feed part). The resync data-loss bug was
    found in QA; no issue filed.` Do not write `Closes #41`: #41 is a broader
    manual QA case that stays open.
