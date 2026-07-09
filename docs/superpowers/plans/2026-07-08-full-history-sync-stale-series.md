# Full-History Sync + Auto-End Stale Recurring Series — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** First sync (and `moneta sync --full`) pulls all available history from the epoch; recurring detection auto-ends series whose newest occurrence is stale (>3 cadence periods old) and auto-reactivates ended series that receive a genuinely new occurrence at cadence.

**Architecture:** Feature 1 is confined to `pipelines/run.py` (`_sync_since`) plus thin plumbing (`POST /sync?full=true`, CLI `--full`). Feature 2 lives in `pipelines/recurring.py`: `detect_recurring` gains a required `today` param; staleness is judged from the group's newest occurrence; "new occurrence" is detected as *the group's newest txn is still untagged* (`series_id IS NULL`) — tagging happens at the end of every run, so an untagged newest txn is by construction new since the last run. Cadence matching moves from whole-group to the *maximal recent run*, because deep history contains breaks that would otherwise poison currently-clean series and make auto-ended reactivation impossible. `emit_series_events` and `power_report` already filter `status == active`, so ended series drop out of missed events and fixed costs; both behaviors get pinned by tests.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, FastAPI, typer, pytest + pytest-asyncio, uv, ruff, mypy --strict.

## Global Constraints

- Verification before every commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — all clean, pristine output (a deprecation warning is a failure).
- TDD: write the failing test, watch it fail, implement, watch it pass, commit.
- Money is integer cents; sign convention negative = outflow.
- Enum columns load as plain `str`; compare with `==`, never `is`.
- Pipelines commit; views don't.
- LLM output never supplies a money value.
- `detect_recurring` must never rewind `next_expected_on` (the `max()` stays; regression test `test_resync_does_not_duplicate_missed_events` pins it).
- Do NOT touch `~/.config/moneta/moneta.db`. All tests use in-memory SQLite.
- Work in worktree `.worktrees/full-history-sync`, branch `feature/full-history-sync`.

---

### Task 0: Test hermeticity — strip ambient `MONETA_*` env vars

The developer shell exports `MONETA_LLM_MODEL`, which leaks into pydantic `BaseSettings`
and fails `tests/test_config.py::test_defaults`. Tests must not depend on the shell.

**Files:**
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: autouse fixture `_clean_moneta_env`; every later task's test runs are hermetic.

- [ ] **Step 1: Reproduce the failure**

Run: `MONETA_LLM_MODEL=openrouter/x uv run pytest tests/test_config.py -q`
Expected: `test_defaults` FAILS (`assert 'openrouter/x' is None`).

- [ ] **Step 2: Add autouse fixture to `tests/conftest.py`**

```python
import os

@pytest.fixture(autouse=True)
def _clean_moneta_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must be hermetic: the developer's shell may export MONETA_* vars."""
    for key in [k for k in os.environ if k.startswith("MONETA_")]:
        monkeypatch.delenv(key)
```

- [ ] **Step 3: Verify**

Run: `MONETA_LLM_MODEL=openrouter/x uv run pytest -q`
Expected: 92 passed.

- [ ] **Step 4: Full verification + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add tests/conftest.py
git commit -m "test: strip ambient MONETA_* env vars so the suite is hermetic"
```

---

### Task 1: Epoch-based first sync + `full` flag in `run_sync`

**Files:**
- Modify: `src/moneta/pipelines/run.py`
- Test: `tests/test_run.py`

**Interfaces:**
- Produces: `run_sync(session, adapter, llm, today, full: bool = False)`; `_sync_since(session, full)` returns `date(1970, 1, 1)` on empty table or `full=True`, else `newest - RESYNC_OVERLAP_DAYS`. `FIRST_SYNC_DAYS` is deleted (not replaced).
- Consumed by: Task 2 (API passes `full` through).

- [ ] **Step 1: Rewrite `tests/test_run.py` (failing tests)**

```python
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import Snapshot
from moneta.pipelines.run import RESYNC_OVERLAP_DAYS, run_sync
from tests.factories import make_account, make_txn


class RecordingAdapter:
    """Records the `since` value run_sync passes to fetch."""

    def __init__(self) -> None:
        self.since: date | None | str = "never-called"

    async def fetch(self, since: date | None = None) -> Snapshot:
        self.since = since
        return Snapshot(accounts=[], transactions=[], holdings=[])


async def test_first_sync_requests_all_history(session: AsyncSession) -> None:
    adapter = RecordingAdapter()
    await run_sync(session, adapter, llm=None, today=date(2026, 7, 9))
    assert adapter.since == date(1970, 1, 1)


async def test_resync_requests_from_newest_txn_with_overlap(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, posted_on=date(2026, 6, 1))
    await make_txn(session, acct, posted_on=date(2026, 7, 5))
    adapter = RecordingAdapter()
    await run_sync(session, adapter, llm=None, today=date(2026, 7, 9))
    assert adapter.since == date(2026, 7, 5) - timedelta(days=RESYNC_OVERLAP_DAYS)


async def test_full_sync_forces_epoch_pull_despite_existing_txns(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, posted_on=date(2026, 7, 5))
    adapter = RecordingAdapter()
    await run_sync(session, adapter, llm=None, today=date(2026, 7, 9), full=True)
    assert adapter.since == date(1970, 1, 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_run.py -q`
Expected: FAIL (`ImportError` on `FIRST_SYNC_DAYS` removal comes later — first failure is `test_first_sync_requests_all_history` asserting epoch, and `TypeError: unexpected keyword argument 'full'`).

- [ ] **Step 3: Implement in `src/moneta/pipelines/run.py`**

Replace the constants and `_sync_since`/`run_sync`:

```python
# SimpleFIN's default window when start-date is omitted is server-chosen (~1 day), so an
# explicit since is always sent: the epoch on first/full sync (each institution returns
# whatever history it retains), overlap from the newest stored txn otherwise.
_EPOCH = date(1970, 1, 1)
RESYNC_OVERLAP_DAYS = 7


async def _sync_since(session: AsyncSession, full: bool) -> date:
    if full:
        return _EPOCH
    newest = (await session.execute(select(func.max(Transaction.posted_on)))).scalar_one_or_none()
    if newest is None:
        return _EPOCH
    return newest - timedelta(days=RESYNC_OVERLAP_DAYS)


async def run_sync(
    session: AsyncSession,
    adapter: AggregatorAdapter,
    llm: Classifier | None,
    today: date,
    full: bool = False,
) -> SyncReport:
    snap = await adapter.fetch(since=await _sync_since(session, full))
    ...  # rest unchanged
```

`FIRST_SYNC_DAYS` is deleted entirely. (`today` stays: recurring/events need it — Task 3.)

- [ ] **Step 4: Verify + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/run.py tests/test_run.py
git commit -m "feat: first sync pulls all available history from the epoch; add full-resync option"
```

---

### Task 2: `POST /sync?full=true` + `moneta sync --full`

**Files:**
- Modify: `src/moneta/api.py` (sync endpoint), `src/moneta/cli/main.py` (sync command)
- Test: `tests/test_api.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `run_sync(..., full=...)` from Task 1.
- Produces: `POST /sync?full=true` query param; CLI flag `moneta sync --full`.

- [ ] **Step 1: Failing API test (`tests/test_api.py`)**

Also import `timedelta`, `async_sessionmaker`/`AsyncSession` (already imported), and
`RESYNC_OVERLAP_DAYS`. Note the `session` and `client` fixtures share one StaticPool
in-memory DB, so seeding through `session` is visible to the app.

```python
from moneta.pipelines.run import RESYNC_OVERLAP_DAYS


class RecordingAdapter:
    def __init__(self) -> None:
        self.since: date | None = None

    async def fetch(self, since: date | None = None) -> Snapshot:
        self.since = since
        return Snapshot(accounts=[], transactions=[], holdings=[])


async def test_sync_full_param_forces_epoch_pull(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, posted_on=date(2026, 7, 5))
    await session.commit()
    adapter = RecordingAdapter()
    app = create_app(sessionmaker, adapter=adapter, llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/sync")).status_code == 200
        assert adapter.since == date(2026, 7, 5) - timedelta(days=RESYNC_OVERLAP_DAYS)
        assert (await c.post("/sync", params={"full": "true"})).status_code == 200
        assert adapter.since == date(1970, 1, 1)
```

- [ ] **Step 2: Failing CLI test (`tests/test_cli.py`)**

The CLI is a thin client — the test asserts the flag maps to the right request, nothing more.

```python
def test_sync_full_flag_requests_full_sync(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        calls.append((method, path))
        if path.startswith("/sync"):
            return {
                "ingest": {"new_transactions": 0},
                "transfers": {"linked": 0},
                "recurring": {"new_series": 0},
                "events": 0,
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync", "--full"])
    assert result.exit_code == 0
    assert calls[0] == ("POST", "/sync?full=true")
```

- [ ] **Step 3: Run to verify failures**

Run: `uv run pytest tests/test_api.py tests/test_cli.py -q`
Expected: new API test fails (since == newest-overlap after full call — param ignored); CLI test fails (`no such option: --full`).

- [ ] **Step 4: Implement**

`src/moneta/api.py`:

```python
    @app.post("/sync")
    async def sync(session: Session, full: bool = False) -> SyncReport:
        if adapter is None:
            raise HTTPException(
                status_code=400,
                detail="No SimpleFIN aggregator configured. Run: moneta setup simplefin <token>",
            )
        return await run_sync(session, adapter, llm, today=date.today(), full=full)
```

`src/moneta/cli/main.py`:

```python
@app.command()
def sync(
    full: Annotated[
        bool, typer.Option("--full", help="Re-pull all available history (e.g. after linking a new account).")
    ] = False,
) -> None:
    """Pull latest data and run all pipelines."""
    report = request("POST", "/sync?full=true" if full else "/sync")
    ...  # rest unchanged
```

- [ ] **Step 5: Verify + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/api.py src/moneta/cli/main.py tests/test_api.py tests/test_cli.py
git commit -m "feat: moneta sync --full re-pulls all history via POST /sync?full=true"
```

---

### Task 3: `detect_recurring(today)` — stale auto-end + manual-end/reactivation semantics

**Files:**
- Modify: `src/moneta/pipelines/recurring.py`, `src/moneta/pipelines/run.py:47` (pass `today`)
- Test: `tests/test_recurring.py` (new tests + `today=` on every existing call), `tests/test_api.py` (two seeding calls get `today=`; `SNAP` becomes date-relative)

**Interfaces:**
- Produces: `detect_recurring(session, llm, today: date)` (required param); constant `_STALE_PERIODS = 3`. Staleness: `(today - newest_occurrence).days > _STALE_PERIODS * CADENCE_DAYS[cadence]`.
- Semantics: stale ⇒ `status=ended` on create and update (`next_expected_on` still computed/advanced, never rewound). Ended + fresh **untagged** newest txn ⇒ `status=active`. Manually ended + no new txn ⇒ stays ended. Status change counts toward `stats.updated`.

- [ ] **Step 1: Add failing tests to `tests/test_recurring.py`**

```python
from moneta.models import SeriesStatus  # extend existing import block


async def test_stale_history_creates_ended_series(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2025, month, 15)
        )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended
    assert s.next_expected_on == date(2025, 4, 14)  # still computed: 3/15 + 30


async def test_active_series_auto_ends_when_stale(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.active
    stats = await detect_recurring(session, llm=None, today=date(2026, 11, 1))  # 139 days stale
    assert stats.updated == 1
    assert s.status == SeriesStatus.ended


async def test_manually_ended_series_stays_ended_without_new_activity(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    s.status = SeriesStatus.ended  # what PATCH /recurring/{id} does
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert s.status == SeriesStatus.ended


async def test_manually_ended_series_reactivates_on_new_occurrence(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    s.status = SeriesStatus.ended
    await session.commit()
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 7, 15)
    )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 16))
    assert stats.updated == 1
    assert s.status == SeriesStatus.active
    assert s.next_expected_on == date(2026, 8, 14)


async def test_backfilled_old_txns_do_not_reactivate_ended_series(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    s.status = SeriesStatus.ended
    await session.commit()
    # a deep re-pull (sync --full) backfills an OLDER txn; newest occurrence is unchanged
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2026, 3, 15)
    )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert s.status == SeriesStatus.ended
```

- [ ] **Step 2: Update every existing `detect_recurring` call**

`tests/test_recurring.py`: add `today=date(2026, 7, 1)` to all existing calls except:
- `test_rerun_updates_not_duplicates`: second call `today=date(2026, 7, 16)` (new txn 7/15).
- `test_resync_does_not_duplicate_missed_events`: both calls `today=today` (the existing `today = date(2026, 5, 1)` local; newest txn 3/15 is 47 days old — not stale, semantics preserved).

`tests/test_api.py`: the two direct seeding calls get `today=date(2026, 7, 1)`
(their seeded txns are in 2026-04..06 — check the surrounding test data when editing).

`tests/test_api.py` `SNAP`: replace pinned txn dates with date-relative ones (the repo
convention — `/sync` and `/power` resolve `date.today()` at request time, and stale
auto-ending would end this series once real time passes 2026-09):

```python
_TODAY = date.today()

SNAP = Snapshot(
    accounts=[...unchanged...],
    transactions=[
        TransactionDTO(
            id=f"TRN-{i}",
            account_id="ACT-1",
            posted_on=_TODAY - timedelta(days=days_ago),
            amount=Decimal("-15.99"),
            description="NETFLIX.COM",
            raw={},
        )
        for i, days_ago in enumerate((75, 45, 15))
    ],
    holdings=[],
)
```

(Any test asserting on those txn dates gets adjusted the same way; `balance_date` may stay pinned.)

`src/moneta/pipelines/run.py:47`: `recurring = await detect_recurring(session, llm, today)`.

- [ ] **Step 3: Run to verify failures**

Run: `uv run pytest tests/test_recurring.py -q`
Expected: new tests FAIL (`TypeError: detect_recurring() got an unexpected keyword argument 'today'`).

- [ ] **Step 4: Implement in `src/moneta/pipelines/recurring.py`**

Add constant next to `_MIN_OCCURRENCES`:

```python
_STALE_PERIODS = 3
```

Signature:

```python
async def detect_recurring(
    session: AsyncSession, llm: Classifier | None, today: date
) -> RecurringStats:
```

In the per-group loop, after `next_on` is computed, replace the create/update block:

```python
        next_on = dates[-1] + timedelta(days=CADENCE_DAYS[cadence])
        stale = (today - dates[-1]).days > _STALE_PERIODS * CADENCE_DAYS[cadence]
        series = existing.get((merchant, direction))
        if series is None:
            series = RecurringSeries(
                merchant=merchant,
                direction=direction,
                cadence=cadence,
                expected_cents=expected,
                next_expected_on=next_on,
                status=SeriesStatus.ended if stale else SeriesStatus.active,
            )
            session.add(series)
            await session.flush()
            session.add(
                SeriesEvent(
                    series_id=series.id,
                    kind=EventKind.new_series,
                    occurred_on=dates[-1],
                    details={"merchant": merchant},
                )
            )
            stats.new_series += 1
        else:
            advanced_on = max(series.next_expected_on, next_on)
            if stale:
                status = SeriesStatus.ended
            elif series.status == SeriesStatus.ended and group[-1].series_id is None:
                # the newest occurrence is untagged ⇒ genuinely new since the last run:
                # an ended series (auto- or manually) that charges again at cadence revives
                status = SeriesStatus.active
            else:
                status = series.status
            changed = (
                series.next_expected_on != advanced_on
                or series.cadence != cadence
                or series.status != status
            )
            series.cadence = cadence
            series.next_expected_on = advanced_on
            series.status = status
            if changed:
                stats.updated += 1
```

(`max()` on `next_expected_on` is untouched — never-rewind survives.)

- [ ] **Step 5: Verify + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/recurring.py src/moneta/pipelines/run.py tests/test_recurring.py tests/test_api.py
git commit -m "feat: auto-end stale recurring series; reactivate on new occurrence at cadence"
```

---

### Task 4: Cadence on the maximal recent run (deep-history robustness + auto-ended reactivation)

Whole-group cadence matching breaks under full history: one break anywhere in years of
data (pause, resubscribe, card reissue) fails `all(gaps)` and the series is silently
never detected — and an *auto-ended* series could never reactivate, because the gap that
made it stale poisons the group forever. Judge cadence on the maximal recent run instead;
amount stats come from that run too (old price epochs stop skewing `expected_cents`).

**Files:**
- Modify: `src/moneta/pipelines/recurring.py` (`_match_cadence`, group loop)
- Test: `tests/test_recurring.py`

**Interfaces:**
- Produces: `_match_cadence(dates) -> tuple[Cadence, int] | None` (cadence + start index of the newest matching run). Loop uses `run = group[start:]` for amounts; dates/staleness/tagging unchanged (tagging still covers the whole group).

- [ ] **Step 1: Add failing tests to `tests/test_recurring.py`**

```python
async def test_historical_gap_does_not_poison_current_cadence(session: AsyncSession) -> None:
    acct = await make_account(session)
    # an old run at one price, a long break, then a current run at a new price
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2025, month, 15)
        )
    for month in (4, 5, 6):
        await make_txn(
            session, acct, amount_cents=-1799, merchant="Netflix", posted_on=date(2026, month, 15)
        )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.active
    assert s.expected_cents == -1799  # median of the current run, not the whole history
    assert s.next_expected_on == date(2026, 7, 15)


async def test_auto_ended_series_reactivates_when_cadence_reestablished(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Hulu", posted_on=date(2025, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    s = (await _series(session))[0]
    assert s.status == SeriesStatus.ended  # auto-ended: stale history
    # resubscribed: three fresh occurrences re-establish the cadence
    for day in (date(2026, 5, 1), date(2026, 6, 1), date(2026, 7, 1)):
        await make_txn(session, acct, amount_cents=-1899, merchant="Hulu", posted_on=day)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 2))
    assert stats.updated == 1
    assert s.status == SeriesStatus.active
    assert s.next_expected_on == date(2026, 7, 31)
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_recurring.py -q`
Expected: both FAIL — whole-group gaps include the break, `_match_cadence` returns None, no series / no reactivation.

- [ ] **Step 3: Implement in `src/moneta/pipelines/recurring.py`**

```python
def _match_cadence(dates: list[date]) -> tuple[Cadence, int] | None:
    """Best cadence and the start index of the newest run matching it.

    Deep history contains breaks (pauses, resubscriptions, card reissues); judging
    cadence on the maximal recent run keeps ancient gaps from poisoning a
    currently-clean series.
    """
    for cadence, days in CADENCE_DAYS.items():
        tol = _TOLERANCE[cadence]
        start = len(dates) - 1
        while start > 0 and abs((dates[start] - dates[start - 1]).days - days) <= tol * 2:
            start -= 1
        run = dates[start:]
        if len(run) < _MIN_OCCURRENCES:
            continue
        gaps = [(b - a).days for a, b in zip(run, run[1:], strict=False)]
        if abs(statistics.median(gaps) - days) <= tol:
            return cadence, start
    return None
```

In the loop:

```python
        match = _match_cadence(dates)
        if match is None:
            continue
        cadence, start = match
        run = group[start:]
        amounts = [abs(t.amount_cents) for t in run]
```

(`dates[-1]`, staleness, `next_on`, and whole-group tagging are unchanged; a clean group
has `start == 0`, so every existing test's semantics are identical.)

- [ ] **Step 4: Verify + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/recurring.py tests/test_recurring.py
git commit -m "feat: judge cadence on the newest run so history breaks don't poison detection"
```

---

### Task 5: Pin the downstream guarantees — no missed events, no fixed costs for auto-ended series

**Files:**
- Test: `tests/test_events.py`, `tests/test_power.py` (tests only; behavior already falls out of the `status == active` filters — these pin it per spec)

- [ ] **Step 1: Add test to `tests/test_events.py`**

```python
from moneta.pipelines.recurring import detect_recurring  # extend imports


async def test_auto_ended_series_emits_no_missed_events(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-1599, merchant="Netflix", posted_on=date(2025, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    assert await emit_series_events(session, today=date(2026, 7, 8)) == 0
    missed = (
        (await session.execute(select(SeriesEvent).where(SeriesEvent.kind == EventKind.missed)))
        .scalars()
        .all()
    )
    assert missed == []
```

- [ ] **Step 2: Add test to `tests/test_power.py`**

```python
from moneta.pipelines.recurring import detect_recurring  # extend imports


async def test_stale_series_never_appears_in_fixed_costs(session: AsyncSession) -> None:
    acct = await make_account(session, type=AccountType.checking)
    for month in (1, 2, 3):
        await make_txn(
            session, acct, amount_cents=-4999, merchant="Dead Gym", posted_on=date(2025, month, 15)
        )
    await detect_recurring(session, llm=None, today=date(2026, 7, 8))
    report = await power_report(session, today=date(2026, 7, 8))
    assert report.fixed_costs == []
    assert report.total_fixed == Decimal(0)
```

- [ ] **Step 3: Run — both must pass immediately** (the filters exist; these are regression pins)

Run: `uv run pytest tests/test_events.py tests/test_power.py -q`
Expected: PASS. If either fails, the filter regressed — fix before proceeding.

- [ ] **Step 4: Verify + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add tests/test_events.py tests/test_power.py
git commit -m "test: pin that auto-ended series emit no missed events and skip fixed costs"
```

---

### Task 6: Docs

**Files:**
- Modify: `README.md` (mention `moneta sync --full`), `CLAUDE.md` (conventions: stale auto-end + reactivation semantics, epoch first sync)

- [ ] **Step 1: README** — in the quickstart near `uv run moneta sync`, add a line: first sync pulls all available history; `moneta sync --full` re-pulls everything (use after linking a new account).

- [ ] **Step 2: CLAUDE.md** — extend the pipeline-order convention bullet with: first sync and `--full` pull from the epoch (`date(1970, 1, 1)`) — there is no window constant; `detect_recurring` auto-ends series whose newest occurrence is >3 cadence periods before `today` and reactivates an ended series only when the group's newest txn is untagged (genuinely new) and fresh; cadence is judged on the newest run, not the whole group.

- [ ] **Step 3: Verify + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add README.md CLAUDE.md
git commit -m "docs: full-history sync and stale-series semantics"
```

---

### Task 7: Reviews and wrap-up (per house rules)

- [ ] code-architect review (`feature-dev:code-architect` agent) over the branch diff — fix every finding, re-verify, commit.
- [ ] `/simplify` over the changed code — apply every finding, re-verify, commit.
- [ ] QA backlog items in `docs/qa-backlog/` (template per global CLAUDE.md): deep `sync --full` against the real SimpleFIN bridge; stale-series behavior on real history.
- [ ] `claude-md-management:claude-md-improver` audit.
- [ ] superpowers:verification-before-completion — run the full gate, confirm output.
- [ ] Push branch, open PR to `main` (repo `anirudhlath/moneta`).

## Self-Review Notes

- Spec coverage: epoch first sync ✓ (Task 1), FIRST_SYNC_DAYS removed not replaced ✓ (Task 1), resync unchanged ✓ (Task 1 test), `--full` end-to-end ✓ (Task 2), test_run.py updated ✓ (Task 1), `today` param via run_sync ✓ (Task 3), stale ⇒ ended on create/update with next_expected_on computed ✓ (Task 3), reactivation auto+manual ✓ (Tasks 3–4), manual-end stays ended ✓ (Task 3), no missed events for auto-ended ✓ (Task 5), stale never in fixed costs ✓ (Task 5), never-rewind survives ✓ (Task 3 keeps `max()`; regression test updated with `today=today`).
- Judgment addition, flagged for the PR description: run-suffix cadence matching (Task 4). Required for auto-ended reactivation (the stale gap otherwise poisons the group forever) and for detection quality under deep history.
- Collateral fixes: hermetic env (Task 0); `tests/test_api.py` SNAP pinned dates → date-relative (Task 3) per the repo's own anchoring convention, since stale logic turns them into a time-bomb.
