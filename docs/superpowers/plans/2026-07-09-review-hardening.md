# Review Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 14 improvement areas from the 2026-07-09 whole-repo review: recurring/events correctness, ingest update-on-match, local-timezone dates, multi-currency guard, config hardening, Alembic migrations, SQLite pragmas, API auth, backup, sync audit trail + logging, coverage tests, backlog cleanup.

**Architecture:** All changes stay inside the existing layering (aggregator → pipelines → views, API owns endpoints, CLI stays a thin client). Two new modules: `src/moneta/migrations/` (Alembic, run programmatically by `init_db`) and `src/moneta/logs.py` (loguru sink setup). One new table: `sync_runs`.

**Tech Stack:** Python 3.13, uv, SQLAlchemy 2 async + aiosqlite, FastAPI, typer, loguru, alembic (new), tomli-w (new).

## Global Constraints

- Verification gate before EVERY commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — all four must pass, output pristine (a deprecation warning is a failure).
- Money is integer cents (`*_cents: int`); negative = outflow.
- Enum columns load as plain `str`; compare with `==`, never `is`.
- Pipelines commit; views don't. LLM output is classification only.
- `detect_recurring` must never rewind `next_expected_on`.
- Package manager is `uv` (`uv add <dep>`, `uv run <cmd>`). Line length 100.
- Working dir: `/Users/anirudhlath/code/private/moneta/.claude/worktrees/bridge-cse_015Yw5mWzkkMkoCR9nKjkn3e`.

---

### Task 1: Calendar-aware cadence stepping

Fixed 30/365-day arithmetic drifts off the real day-of-month (~5 days/year for monthly), eventually causing false `missed` events. Add `advance_expected_on` and use it wherever `next_expected_on` is projected.

**Files:**
- Modify: `src/moneta/pipelines/recurring.py` (add helper near `CADENCE_DAYS`; use at line 207)
- Modify: `src/moneta/pipelines/events.py:62` (use helper)
- Test: `tests/test_recurring.py`, `tests/test_events.py`

**Interfaces:**
- Produces: `advance_expected_on(d: date, cadence: Cadence) -> date` in `moneta.pipelines.recurring` — later tasks (2) call it.
- `CADENCE_DAYS` stays (still used for gap heuristics/staleness).

- [ ] **Step 1: Write failing tests** (append to `tests/test_recurring.py`):

```python
from moneta.pipelines.recurring import advance_expected_on


def test_advance_monthly_keeps_day_of_month() -> None:
    assert advance_expected_on(date(2026, 1, 1), Cadence.monthly) == date(2026, 2, 1)
    assert advance_expected_on(date(2026, 1, 31), Cadence.monthly) == date(2026, 2, 28)
    assert advance_expected_on(date(2026, 4, 30), Cadence.monthly) == date(2026, 5, 30)


def test_advance_annual_and_fixed_cadences() -> None:
    assert advance_expected_on(date(2026, 2, 28), Cadence.annual) == date(2027, 2, 28)
    assert advance_expected_on(date(2026, 7, 1), Cadence.weekly) == date(2026, 7, 8)
    assert advance_expected_on(date(2026, 7, 1), Cadence.biweekly) == date(2026, 7, 15)
```

(Import `date` / `Cadence` already present in that file; add only what's missing.)

- [ ] **Step 2: Run** `uv run pytest tests/test_recurring.py -q` — expect FAIL (ImportError).

- [ ] **Step 3: Implement** in `src/moneta/pipelines/recurring.py`. Add `from calendar import monthrange` to imports, then below `CADENCE_DAYS`:

```python
def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def advance_expected_on(d: date, cadence: Cadence) -> date:
    """One period after d — calendar-aware for monthly/annual so day-of-month holds."""
    match cadence:
        case Cadence.weekly:
            return d + timedelta(days=7)
        case Cadence.biweekly:
            return d + timedelta(days=14)
        case Cadence.monthly:
            return _add_months(d, 1)
        case Cadence.annual:
            return _add_months(d, 12)
```

Change line 207 `next_on = dates[-1] + timedelta(days=CADENCE_DAYS[cadence])` → `next_on = advance_expected_on(dates[-1], cadence)`.

In `src/moneta/pipelines/events.py`: import `advance_expected_on` alongside `CADENCE_DAYS`, delete `period = timedelta(days=CADENCE_DAYS[s.cadence])` (line 38), and change line 62 to `s.next_expected_on = advance_expected_on(s.next_expected_on, s.cadence)`. If `CADENCE_DAYS` is then unused in events.py, drop that import.

- [ ] **Step 4: Run full suite** `uv run pytest -q`. If any test pins a `next_expected_on` across a 31/28-day month boundary it may shift by 1–3 days — update those expected dates to the calendar-aware value (the new behavior is correct).

- [ ] **Step 5: Verify gate + commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add -A && git commit -m "fix: calendar-aware monthly/annual stepping for next_expected_on"
```

---

### Task 2: Missed-payment catch-up loop

`emit_series_events` advances only one period per sync, so a long-dead series needs N syncs to catch up and `next_expected_on` lags reality.

**Files:**
- Modify: `src/moneta/pipelines/events.py:40-62`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: `advance_expected_on` from Task 1.
- Behavior contract: one `missed` event per empty grace window between old `next_expected_on` and `today`; windows containing a tagged txn advance silently; never rewinds.

- [ ] **Step 1: Write failing test** (append to `tests/test_events.py`):

```python
async def test_missed_payments_catch_up_all_periods(session: AsyncSession) -> None:
    s = await make_series(session, next_expected_on=date(2026, 3, 15))
    n = await emit_series_events(session, today=date(2026, 7, 1))
    assert n == 4  # 3/15, 4/15, 5/15, 6/15 all missed in one sync
    assert s.next_expected_on == date(2026, 7, 15)


async def test_catch_up_skips_windows_with_payment(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 5, 15))
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix",
        posted_on=date(2026, 5, 16), series_id=s.id,
    )
    n = await emit_series_events(session, today=date(2026, 7, 1))
    assert n == 1  # 5/15 window was paid; only 6/15 missed
    assert s.next_expected_on == date(2026, 7, 15)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_events.py -q` — expect the first new test to FAIL (n == 1, next stuck at 4/15).

- [ ] **Step 3: Implement** — in `emit_series_events`, replace the `if today > s.next_expected_on + grace:` block (lines 40–62) with a loop:

```python
        while today > s.next_expected_on + grace:
            window_hit = (
                await session.execute(
                    select(Transaction.id)
                    .where(
                        Transaction.series_id == s.id,
                        Transaction.posted_on >= s.next_expected_on - grace,
                        Transaction.posted_on <= s.next_expected_on + grace,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if window_hit is None:
                session.add(
                    SeriesEvent(
                        series_id=s.id,
                        kind=EventKind.missed,
                        occurred_on=s.next_expected_on,
                        details={"expected_on": s.next_expected_on.isoformat()},
                    )
                )
                emitted += 1
            s.next_expected_on = advance_expected_on(s.next_expected_on, s.cadence)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_events.py -q` — expect PASS (existing `test_missed_payment_emits_once_and_advances` still passes: one window, then loop exits).

- [ ] **Step 5: Verify gate + commit** (`fix: emit a missed event per skipped period, catching up in one sync`)

---

### Task 3: Price change requires two agreeing occurrences

One outlier txn currently overwrites `expected_cents` (which feeds `monthly_cents` → power/fixed costs). Require the two newest occurrences to agree on the new price.

**Files:**
- Modify: `src/moneta/pipelines/events.py:64-84`
- Test: `tests/test_events.py` (update `test_price_increase_detected`, `test_small_variation_not_price_increase`; add outlier test)

**Interfaces:**
- Behavior contract: `price_increase` fires only when the two newest series txns each drift >5% from `expected_cents` and are within 5% of each other; `expected_cents` becomes the newest amount.

- [ ] **Step 1: Update/add tests** in `tests/test_events.py`. Replace `test_price_increase_detected` and `test_small_variation_not_price_increase` with:

```python
async def test_price_increase_detected_after_two_occurrences(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    await make_txn(
        session, acct, amount_cents=-1899, merchant="Netflix",
        posted_on=date(2026, 6, 15), series_id=s.id,
    )
    await make_txn(
        session, acct, amount_cents=-1899, merchant="Netflix",
        posted_on=date(2026, 7, 15), series_id=s.id,
    )
    n = await emit_series_events(session, today=date(2026, 7, 16))
    assert n == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.price_increase
    assert ev.details == {"old_cents": -1599, "new_cents": -1899}
    assert s.expected_cents == -1899
    assert await emit_series_events(session, today=date(2026, 7, 16)) == 0


async def test_first_occurrence_at_new_price_waits(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    await make_txn(
        session, acct, amount_cents=-1899, merchant="Netflix",
        posted_on=date(2026, 7, 15), series_id=s.id,
    )
    assert await emit_series_events(session, today=date(2026, 7, 16)) == 0
    assert s.expected_cents == -1599


async def test_single_outlier_does_not_corrupt_expected(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    await make_txn(
        session, acct, amount_cents=-1599, merchant="Netflix",
        posted_on=date(2026, 6, 15), series_id=s.id,
    )
    await make_txn(
        session, acct, amount_cents=-9999, merchant="Netflix",
        posted_on=date(2026, 7, 15), series_id=s.id,
    )
    assert await emit_series_events(session, today=date(2026, 7, 16)) == 0
    assert s.expected_cents == -1599


async def test_small_variation_not_price_increase(session: AsyncSession) -> None:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    for month in (6, 7):
        await make_txn(
            session, acct, amount_cents=-1620, merchant="Netflix",
            posted_on=date(2026, month, 15), series_id=s.id,
        )  # +1.3% — under the 5% threshold
    assert await emit_series_events(session, today=date(2026, 7, 16)) == 0
```

- [ ] **Step 2: Run** `uv run pytest tests/test_events.py -q` — new tests FAIL against single-sample logic.

- [ ] **Step 3: Implement** — replace the `latest = ...` block (lines 64–84) with:

```python
        latest_two = (
            (
                await session.execute(
                    select(Transaction)
                    .where(Transaction.series_id == s.id)
                    .order_by(Transaction.posted_on.desc(), Transaction.id.desc())
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        # one sample is an outlier until a second occurrence confirms the new price
        if len(latest_two) == 2 and s.expected_cents != 0:
            newest, prior = latest_two
            drift = abs(newest.amount_cents - s.expected_cents) / abs(s.expected_cents)
            settled = (
                abs(newest.amount_cents - prior.amount_cents)
                <= abs(newest.amount_cents) * _PRICE_CHANGE_THRESHOLD
            )
            if drift > _PRICE_CHANGE_THRESHOLD and settled:
                session.add(
                    SeriesEvent(
                        series_id=s.id,
                        kind=EventKind.price_increase,
                        occurred_on=newest.posted_on,
                        details={"old_cents": s.expected_cents, "new_cents": newest.amount_cents},
                    )
                )
                s.expected_cents = newest.amount_cents
                emitted += 1
```

- [ ] **Step 4: Run full suite**; fix any other test that relied on single-sample price events (check `tests/test_e2e.py`).

- [ ] **Step 5: Verify gate + commit** (`fix: price-change events need two agreeing occurrences, not one outlier`)

---

### Task 4: Ingest updates re-synced transactions

Institutions re-post corrected amounts under the same id; ingest currently drops them forever.

**Files:**
- Modify: `src/moneta/pipelines/ingest.py:57-80`, `src/moneta/cli/main.py` (sync output)
- Test: `tests/test_ingest.py`

**Interfaces:**
- Produces: `IngestStats.updated_transactions: int` (new field, default 0). CLI reads `report["ingest"].get("updated_transactions")`.

- [ ] **Step 1: Write failing test** (append to `tests/test_ingest.py`, mirroring its existing Snapshot/DTO style):

```python
async def test_resynced_txn_with_changed_fields_is_updated(session: AsyncSession) -> None:
    acct = AccountDTO(
        id="A-1", name="Checking", org_name="Chase", currency="USD",
        balance=Decimal("100.00"), balance_date=date(2026, 7, 1),
    )

    def snap(amount: str, desc: str) -> Snapshot:
        return Snapshot(
            accounts=[acct],
            transactions=[
                TransactionDTO(
                    id="T-1", account_id="A-1", posted_on=date(2026, 7, 1),
                    amount=Decimal(amount), description=desc, raw={},
                )
            ],
            holdings=[],
        )

    await ingest_snapshot(session, snap("-10.00", "COFFEE"))
    stats = await ingest_snapshot(session, snap("-12.34", "COFFEE SHOP"))
    assert stats.new_transactions == 0
    assert stats.updated_transactions == 1
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.amount_cents == -1234
    assert txn.description == "COFFEE SHOP"

    stats = await ingest_snapshot(session, snap("-12.34", "COFFEE SHOP"))
    assert stats.updated_transactions == 0  # identical re-sync is a no-op
```

(Add any missing imports at top of the file: `Decimal`, `AccountDTO`, `TransactionDTO`, `Snapshot`, `select`, `Transaction`.)

- [ ] **Step 2: Run** — FAIL (`updated_transactions` doesn't exist).

- [ ] **Step 3: Implement** in `src/moneta/pipelines/ingest.py`:

Add to `IngestStats`: `updated_transactions: int = 0`.

Replace the `seen` set + txn loop (lines 57–80) with:

```python
    existing_txns = {
        (t.account_id, t.aggregator_id): t
        for t in (await session.execute(select(Transaction))).scalars()
    }
    for txn in snap.transactions:
        if txn.account_id not in acct_ids:
            continue
        key = (acct_ids[txn.account_id], txn.id)
        row = existing_txns.get(key)
        if row is not None:
            cents = to_cents(txn.amount)
            fields = (cents, txn.posted_on, txn.description)
            if fields != (row.amount_cents, row.posted_on, row.description):
                row.amount_cents, row.posted_on, row.description = fields
                row.raw = txn.raw
                stats.updated_transactions += 1
            continue
        row = Transaction(
            account_id=key[0],
            aggregator_id=txn.id,
            posted_on=txn.posted_on,
            amount_cents=to_cents(txn.amount),
            description=txn.description,
            raw=txn.raw,
        )
        session.add(row)
        existing_txns[key] = row
        stats.new_transactions += 1
```

In `src/moneta/cli/main.py` `sync()`, after the auto_resolved line add:

```python
    if report["ingest"].get("updated_transactions"):
        console.print(
            f"{report['ingest']['updated_transactions']} transaction(s) corrected upstream."
        )
```

- [ ] **Step 4: Run full suite** (the existing `test_ingest_is_idempotent` must still pass).

- [ ] **Step 5: Verify gate + commit** (`fix: ingest updates re-synced transactions whose fields changed upstream`)

---

### Task 5: Local-timezone posted dates

`_ts_to_date` uses UTC, so evening purchases land on the next day and month windows misattribute spend.

**Files:**
- Modify: `src/moneta/aggregator/simplefin.py:25-26`
- Modify: `tests/conftest.py` (pin TZ=UTC for determinism)
- Test: `tests/test_simplefin.py`

**Interfaces:**
- `_ts_to_date(ts: int) -> date` now converts in the process's local timezone. Tests are pinned to UTC by an autouse fixture; a dedicated test overrides TZ.

- [ ] **Step 1: Add autouse TZ fixture** to `tests/conftest.py` (add `import time` at top):

```python
@pytest.fixture(autouse=True)
def _utc_timezone() -> Iterator[None]:
    """_ts_to_date converts in local time; pin TZ so tests don't depend on the machine."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()
```

(`Iterator` from `collections.abc`.)

- [ ] **Step 2: Write failing test** (append to `tests/test_simplefin.py`, add `import os, time` and `from moneta.aggregator.simplefin import _ts_to_date`):

```python
def test_ts_to_date_uses_local_timezone() -> None:
    os.environ["TZ"] = "America/Los_Angeles"  # autouse fixture restores after the test
    time.tzset()
    # 1782963000 == 2026-07-02T03:30:00Z == 2026-07-01 20:30 in Los Angeles
    assert _ts_to_date(1782963000) == date(2026, 7, 1)


def test_ts_to_date_utc() -> None:  # TZ pinned to UTC by the autouse fixture
    assert _ts_to_date(1782963000) == date(2026, 7, 2)
```

- [ ] **Step 3: Run** — LA test FAILS (returns 7/2 under UTC conversion).

- [ ] **Step 4: Implement**: in `simplefin.py` change

```python
def _ts_to_date(ts: int) -> date:
    return datetime.fromtimestamp(ts).date()  # local tz: the user's calendar day
```

(`UTC` import stays — `fetch` still builds the start-date param in UTC, which is fine for a 7-day-overlap window.)

- [ ] **Step 5: Run full suite** — existing simplefin tests stay green because the fixture pins UTC.

- [ ] **Step 6: Verify gate + commit** (`fix: convert SimpleFIN timestamps to local-timezone dates`)

---

### Task 6: Multi-currency guard in views

Views sum cents across currencies blindly. Filter aggregation to the primary (majority) currency and surface excluded accounts.

**Files:**
- Modify: `src/moneta/queries.py` (add `primary_currency`), `src/moneta/views/networth.py`, `src/moneta/views/power.py`, `src/moneta/views/cashflow.py`, `src/moneta/cli/main.py` (networth warning)
- Test: `tests/test_networth.py`, `tests/test_power.py`

**Interfaces:**
- Produces: `async def primary_currency(session: AsyncSession) -> str` in `moneta.queries` (majority currency; ties prefer "USD", then alphabetical; "USD" when no accounts).
- `NetWorthReport.foreign_accounts: int` (new field).
- Known limitation (fine for now): `RecurringSeries` has no currency; series stats still aggregate across accounts.

- [ ] **Step 1: Write failing tests**:

Append to `tests/test_networth.py`:

```python
async def test_foreign_currency_accounts_excluded(session: AsyncSession) -> None:
    await make_account(session, type=AccountType.checking, balance_cents=100_000)
    await make_account(
        session, type=AccountType.checking, balance_cents=55_500, currency="EUR"
    )
    r = await net_worth_report(session)
    assert r.liquid == Decimal("1000.00")
    assert r.foreign_accounts == 1
```

Append to `tests/test_power.py`:

```python
async def test_spent_ignores_foreign_currency_accounts(session: AsyncSession) -> None:
    usd = await make_account(session, type=AccountType.checking)
    eur = await make_account(session, type=AccountType.checking, currency="EUR")
    await make_txn(session, usd, amount_cents=-5000, posted_on=date(2026, 7, 3))
    await make_txn(session, eur, amount_cents=-7000, posted_on=date(2026, 7, 4))
    r = await power_report(session, today=date(2026, 7, 9))
    assert r.spent_so_far == Decimal("50.00")
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement**:

`queries.py` (add `func` to the sqlalchemy import):

```python
async def primary_currency(session: AsyncSession) -> str:
    """Majority currency across accounts; ties prefer USD. Views aggregate only this."""
    rows = (
        await session.execute(select(Account.currency, func.count()).group_by(Account.currency))
    ).all()
    if not rows:
        return "USD"
    return max(rows, key=lambda r: (r[1], r[0] == "USD", r[0]))[0]
```

`networth.py` — add `foreign_accounts: int` to `NetWorthReport`; rewrite the body:

```python
async def net_worth_report(session: AsyncSession) -> NetWorthReport:
    primary = await primary_currency(session)
    accounts = (await session.execute(select(Account))).scalars().all()
    domestic = [a for a in accounts if a.currency == primary]
    liquid = sum(a.balance_cents for a in domestic if a.type in LIQUID_ACCOUNT_TYPES)
    liabilities = sum(abs(a.balance_cents) for a in domestic if a.type in LIABILITY_ACCOUNT_TYPES)
    unknown = sum(1 for a in domestic if a.type == AccountType.unknown)

    vested_cents = 0
    unvested_cents = 0
    holdings = (
        await session.execute(
            select(Holding)
            .join(Account, Holding.account_id == Account.id)
            .where(Account.currency == primary)
        )
    ).scalars()
    for h in holdings:
        ...  # existing vested/unvested arithmetic unchanged

    return NetWorthReport(
        ...,  # existing fields unchanged
        foreign_accounts=len(accounts) - len(domestic),
    )
```

(Keep the existing vested/unvested loop body and report fields exactly as they are; only the holdings query, `domestic` filtering, and the new field change. Import `primary_currency` from `moneta.queries`.)

`power.py` — in `power_report`, compute `primary = await primary_currency(session)` and add `Account.currency == primary` to the `month_txns` where-clause.

`cashflow.py` — in both `accrual_spend` and `cash_out`, compute `primary = await primary_currency(session)` and add `Account.currency == primary` to the where-clause.

`cli/main.py` `networth()` — after the unknown-accounts warning:

```python
    if r["foreign_accounts"]:
        console.print(
            f"[yellow]{r['foreign_accounts']} account(s) in a non-primary currency are "
            f"excluded from these totals[/yellow]"
        )
```

- [ ] **Step 4: Run full suite.**

- [ ] **Step 5: Verify gate + commit** (`fix: views aggregate only the primary currency instead of mixing currencies`)

---

### Task 7: Config hardening — permissions + real TOML writer

The SimpleFIN access URL (full bank read credentials) is written world-readable with naive f-string quoting.

**Files:**
- Modify: `pyproject.toml` (via `uv add tomli-w`), `src/moneta/config.py:44-51`
- Delete: `docs/backlog/low/save-config-toml-escaping.md` (fixed here)
- Test: `tests/test_config.py`

- [ ] **Step 1:** `uv add tomli-w`

- [ ] **Step 2: Write failing tests** (append to `tests/test_config.py`, style-matching its existing monkeypatch use of `MONETA_CONFIG_DIR`):

```python
def test_save_config_value_escapes_and_roundtrips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    tricky = 'https://u:p"w\\x@bridge.example/simplefin'
    save_config_value("simplefin_access_url", tricky)
    assert load_settings().simplefin_access_url == tricky


def test_save_config_value_restricts_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    save_config_value("llm_model", "gpt")
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
```

- [ ] **Step 3: Run** — escaping test FAILS (tomllib parse error or mangled value).

- [ ] **Step 4: Implement** in `config.py` (add `import tomli_w`):

```python
def save_config_value(key: str, value: str) -> None:
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)  # the config file holds bank credentials
    path = config_dir / "config.toml"
    values = _read_config_file(config_dir)
    values[key] = value
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(tomli_w.dumps(values))
    path.chmod(0o600)
```

If mypy lacks stubs for `tomli_w` (it ships typed — it shouldn't), do NOT add an override; check the error first.

- [ ] **Step 5:** `git rm docs/backlog/low/save-config-toml-escaping.md`

- [ ] **Step 6: Verify gate + commit** (`fix: config file written 0600/0700 via a real TOML writer`)

---

### Task 8: SQLite pragmas — WAL + busy_timeout

`moneta serve` and the in-process CLI can write the same file; defaults fail instantly with "database is locked".

**Files:**
- Modify: `src/moneta/db.py`
- Test: `tests/test_models.py` (or new `tests/test_db.py`)

- [ ] **Step 1: Write failing test** (new file `tests/test_db.py`):

```python
from pathlib import Path

from sqlalchemy import text

from moneta.db import init_db, make_sessionmaker


async def test_file_backed_engine_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    engine, sm = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    async with sm() as session:
        assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await session.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
    await engine.dispose()
```

- [ ] **Step 2: Run** — FAIL (journal_mode == "delete").

- [ ] **Step 3: Implement** in `db.py`:

```python
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from moneta.models import Base


def make_sessionmaker(db_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    kwargs: dict[str, object] = {}
    in_memory = db_url.endswith("aiosqlite://")
    if in_memory:  # in-memory: share one connection across sessions
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(db_url, **kwargs)
    if not in_memory:  # serve + in-process CLI may write the same file concurrently

        @event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)
```

(`init_db` unchanged in this task.)

- [ ] **Step 4: Run full suite.**

- [ ] **Step 5: Verify gate + commit** (`fix: enable WAL and busy_timeout on file-backed SQLite engines`)

---

### Task 9: Alembic migrations

`init_db` is `create_all`-only; the first model change against a real DB breaks at query time. Migrate via Alembic, run programmatically; adopt existing pre-migration DBs by stamping the baseline.

**Files:**
- Modify: `pyproject.toml` (via `uv add alembic`), `src/moneta/db.py`
- Create: `src/moneta/migrations/__init__.py` (empty), `src/moneta/migrations/env.py`, `src/moneta/migrations/script.py.mako`, `src/moneta/migrations/versions/__init__.py` (empty), `src/moneta/migrations/versions/0001_baseline.py`
- Test: new `tests/test_migrations.py`

**Interfaces:**
- `init_db(engine)` keeps its signature; internally: stamp `0001` if tables exist without `alembic_version`, then `upgrade head`. `create_all` is no longer called by production code.
- Later tasks add revisions with `down_revision` chaining from `"0001"`.

- [ ] **Step 1:** `uv add alembic`

- [ ] **Step 2: Create the migration environment.**

`src/moneta/migrations/env.py`:

```python
"""Runs only via moneta.db.init_db, which injects a live connection.

New revisions are written by hand in versions/ (id NNNN, down_revision = previous).
"""

from alembic import context
from sqlalchemy.engine import Connection

from moneta.models import Base

connection: Connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
```

`src/moneta/migrations/script.py.mako` (standard template):

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

`src/moneta/migrations/versions/0001_baseline.py`:

```python
"""Baseline: schema as of 2026-07-09 (pre-migration create_all schema)."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("aggregator_id", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("org_name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("balance_cents", sa.Integer(), nullable=False),
        sa.Column("balance_date", sa.Date(), nullable=False),
        sa.Column("promo_expires_on", sa.Date(), nullable=True),
    )
    op.create_table(
        "recurring_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("merchant", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("cadence", sa.String(), nullable=False),
        sa.Column("expected_cents", sa.Integer(), nullable=False),
        sa.Column("next_expected_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.UniqueConstraint("merchant", "direction"),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("aggregator_id", sa.String(), nullable=False),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("merchant", sa.String(), nullable=True),
        sa.Column(
            "series_id", sa.Integer(), sa.ForeignKey("recurring_series.id"), nullable=True
        ),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "aggregator_id"),
    )
    op.create_table(
        "transfer_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "outflow_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "inflow_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
    )
    op.create_table(
        "series_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "series_id", sa.Integer(), sa.ForeignKey("recurring_series.id"), nullable=False
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("market_value_cents", sa.Integer(), nullable=False),
        sa.Column("vested_quantity", sa.Float(), nullable=True),
        sa.Column("unvested_quantity", sa.Float(), nullable=True),
        sa.UniqueConstraint("account_id", "symbol"),
    )
    op.create_table(
        "merchant_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("raw_descriptor", sa.String(), nullable=False, unique=True),
        sa.Column("merchant", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
    )
    op.create_table(
        "review_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=True),
        sa.Column(
            "created_on",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    for table in (
        "review_items",
        "merchant_aliases",
        "holdings",
        "series_events",
        "transfer_links",
        "transactions",
        "recurring_series",
        "accounts",
    ):
        op.drop_table(table)
```

- [ ] **Step 3: Rewrite `init_db`** in `src/moneta/db.py`:

```python
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event, inspect

_BASELINE = "0001"


def _migration_config(conn: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    return cfg


def _upgrade(conn: Connection) -> None:
    cfg = _migration_config(conn)
    insp = inspect(conn)
    if insp.has_table("accounts") and not insp.has_table("alembic_version"):
        command.stamp(cfg, _BASELINE)  # adopt a pre-Alembic database at the baseline
    command.upgrade(cfg, "head")


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
```

(`Base` import may become unused in db.py — remove it if so.)

- [ ] **Step 4: Write tests** (new `tests/test_migrations.py`):

```python
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from moneta.db import init_db, make_sessionmaker
from moneta.models import Base


def _schema(conn: Connection) -> dict[str, list[tuple[str, str, bool]]]:
    insp = inspect(conn)
    return {
        t: [(c["name"], str(c["type"]), bool(c["nullable"])) for c in insp.get_columns(t)]
        for t in insp.get_table_names()
        if t != "alembic_version"
    }


async def _schema_of(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as conn:
        return await conn.run_sync(_schema)


async def test_migrations_match_create_all_schema() -> None:
    via_models, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with via_models.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    via_migrations, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(via_migrations)
    assert await _schema_of(via_models) == await _schema_of(via_migrations)
    await via_models.dispose()
    await via_migrations.dispose()


async def test_init_db_adopts_pre_migration_database() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # simulates a pre-Alembic DB
    await init_db(engine)  # must stamp, then upgrade cleanly (no "table exists" error)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "alembic_version" in tables
    await engine.dispose()


async def test_init_db_is_idempotent() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    await init_db(engine)
    await engine.dispose()
```

- [ ] **Step 5: Run full suite** — every test that calls `init_db` now runs migrations; watch for alembic logging noise in pytest output (must stay pristine — if alembic emits warnings, address the cause, not the filter).

- [ ] **Step 6: Verify gate + commit** (`feat: Alembic migrations; init_db upgrades and adopts pre-migration DBs`)

---

### Task 10: Sync audit trail + logging sink + `moneta status`

`SyncReport` is printed once and discarded; loguru has no sink. Persist every sync run, log to a rotating file, expose `GET /sync/last` and `moneta status`.

**Files:**
- Create: `src/moneta/logs.py`, `src/moneta/migrations/versions/0002_sync_runs.py`
- Modify: `src/moneta/models.py` (SyncRun), `src/moneta/pipelines/run.py`, `src/moneta/api.py` (`/sync/last`, call `configure_logging` in `build_app`), `src/moneta/cli/main.py` (`status` command)
- Test: `tests/test_run.py`, `tests/test_api.py`, `tests/test_cli.py`, `tests/test_logs.py`

**Interfaces:**
- Produces: `SyncRun` model (`sync_runs` table), `configure_logging(config_dir: Path) -> None` (idempotent), `GET /sync/last -> SyncRunOut | None`.
- `run_sync` behavior: writes one `SyncRun` row per invocation — `success=True` + `report` dict on success; `success=False` + `error` string on exception (which re-raises). Domain tables stay untouched on pre-ingest failure.

- [ ] **Step 1: Model + migration.**

Append to `src/moneta/models.py`:

```python
class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    success: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(default=None)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
```

`src/moneta/migrations/versions/0002_sync_runs.py`:

```python
"""Add sync_runs audit table."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "started_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sync_runs")
```

Update `tests/test_migrations.py::test_init_db_adopts_pre_migration_database` so the simulated old DB genuinely predates 0002:

```python
    old_tables = [t for name, t in Base.metadata.tables.items() if name != "sync_runs"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=old_tables))
    await init_db(engine)  # stamps 0001, then 0002 adds sync_runs
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "sync_runs" in tables and "alembic_version" in tables
```

- [ ] **Step 2: `src/moneta/logs.py`:**

```python
"""Loguru sink setup: warnings to stderr, everything to a rotating file."""

import sys
from pathlib import Path

from loguru import logger

_configured = False


def configure_logging(config_dir: Path) -> None:
    global _configured
    if _configured:  # build_app runs once per server but per-command in-process
        return
    _configured = True
    config_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(config_dir / "moneta.log", rotation="10 MB", retention=5, level="INFO")
```

- [ ] **Step 3: `run_sync` records the run.** In `src/moneta/pipelines/run.py` (add imports: `datetime` from datetime, `logger` from loguru, `SyncRun` from moneta.models):

```python
async def run_sync(
    session: AsyncSession,
    adapter: AggregatorAdapter,
    llm: Classifier | None,
    today: date,
    full: bool = False,
) -> SyncReport:
    run = SyncRun(started_at=datetime.now())
    session.add(run)
    await session.commit()
    try:
        snap = await adapter.fetch(since=await _sync_since(session, full))
        ingest = await ingest_snapshot(session, snap)
        normalized = await normalize_merchants(session, llm)
        transfers = await link_transfers(session, llm)
        # auto-review before detection so confident LLM answers (incl. last sync's
        # recurring questions) influence this run's series and exclusions
        auto_resolved = await autoreview_items(session, llm) if llm else 0
        recurring = await detect_recurring(session, llm, today)
        events = await emit_series_events(session, today)
    except Exception as exc:
        await session.rollback()
        run.finished_at = datetime.now()
        run.error = f"{type(exc).__name__}: {exc}"
        await session.commit()
        logger.error("sync failed: {}", run.error)
        raise
    report = SyncReport(
        ingest=ingest,
        normalized=normalized,
        transfers=transfers,
        auto_resolved=auto_resolved,
        recurring=recurring,
        events=events,
    )
    run.finished_at = datetime.now()
    run.success = True
    run.report = report.model_dump(mode="json")
    await session.commit()
    logger.info("sync ok: {}", run.report)
    return report
```

- [ ] **Step 4: API.** In `create_app` add (import `SyncRun`, `datetime`):

```python
class SyncRunOut(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    success: bool
    error: str | None
    report: dict[str, Any] | None
```

```python
    @app.get("/sync/last")
    async def sync_last(session: Session) -> SyncRunOut | None:
        row = (
            await session.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
        ).scalar_one_or_none()
        return SyncRunOut.model_validate(row, from_attributes=True) if row else None
```

In `build_app()` first line: `configure_logging(load_settings().config_dir)` — note `load_settings()` is already called; reuse the variable.

- [ ] **Step 5: CLI.** Add to `src/moneta/cli/main.py`:

```python
@app.command()
def status() -> None:
    """Show the most recent sync run and its outcome."""
    r = request("GET", "/sync/last")
    if not r:
        console.print("No sync has run yet. Run: [bold]moneta sync[/bold]")
        return
    outcome = "[green]ok[/green]" if r["success"] else f"[red]failed[/red] — {r['error']}"
    console.print(f"Last sync: {r['started_at']} → {outcome}")
    if r["report"]:
        rep = r["report"]
        console.print(
            f"  {rep['ingest']['new_transactions']} new txns, "
            f"{rep['recurring']['new_series']} new series, {rep['events']} events"
        )
```

- [ ] **Step 6: Tests.**

Append to `tests/test_run.py`:

```python
async def test_run_sync_records_success_audit_row(session: AsyncSession) -> None:
    await run_sync(session, RecordingAdapter(), llm=None, today=date(2026, 7, 9))
    run = (await session.execute(select(SyncRun))).scalar_one()
    assert run.success is True
    assert run.finished_at is not None
    assert run.report is not None and "ingest" in run.report


async def test_run_sync_records_failure_and_reraises(session: AsyncSession) -> None:
    class FailingAdapter:
        async def fetch(self, since: date | None = None) -> Snapshot:
            raise RuntimeError("bridge down")

    with pytest.raises(RuntimeError, match="bridge down"):
        await run_sync(session, FailingAdapter(), llm=None, today=date(2026, 7, 9))
    run = (await session.execute(select(SyncRun))).scalar_one()
    assert run.success is False
    assert run.error is not None and "bridge down" in run.error
```

(Imports: `pytest`, `select`, `Snapshot`, `SyncRun`.)

Append to `tests/test_api.py`:

```python
async def test_sync_last_endpoint(client: httpx.AsyncClient) -> None:
    assert (await client.get("/sync/last")).json() is None
    await client.post("/sync")
    body = (await client.get("/sync/last")).json()
    assert body["success"] is True
    assert body["report"]["ingest"]["new_transactions"] == 3
```

Append to `tests/test_cli.py`:

```python
def test_status_before_any_sync(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No sync has run yet" in result.output
```

New `tests/test_logs.py`:

```python
from pathlib import Path

import pytest
from loguru import logger

import moneta.logs
from moneta.logs import configure_logging


def test_configure_logging_writes_file_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(moneta.logs, "_configured", False)
    configure_logging(tmp_path)
    configure_logging(tmp_path)  # second call must not add duplicate sinks
    logger.info("hello")
    text = (tmp_path / "moneta.log").read_text()
    assert text.count("hello") == 1
```

- [ ] **Step 7: Run full suite** — note `test_sync_then_views`-style tests now create a `SyncRun` row; nothing asserts on table counts there, so they should pass unchanged.

- [ ] **Step 8: Verify gate + commit** (`feat: sync audit trail (sync_runs), rotating log file, moneta status`)

---

### Task 11: API bearer-token auth

Every mutating endpoint is unauthenticated; `--host 0.0.0.0` is one flag away.

**Files:**
- Modify: `src/moneta/config.py` (Settings), `src/moneta/api.py` (`create_app` param + dependency), `src/moneta/cli/client.py` (header), `src/moneta/cli/main.py` (`serve` guard)
- Test: `tests/test_api.py`, `tests/test_cli.py`

**Interfaces:**
- `Settings.api_token: str | None = None` (env `MONETA_API_TOKEN` or config key `api_token`).
- `create_app(..., api_token: str | None = None)` — when set, every route requires `Authorization: Bearer <token>` else 401. When None, open (in-process default).
- `moneta serve` exits 1 for non-loopback `--host` without a configured token.

- [ ] **Step 1: Failing tests.**

Append to `tests/test_api.py`:

```python
async def test_bearer_token_enforced_when_configured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(sessionmaker, adapter=None, llm=None, api_token="s3cret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/accounts")).status_code == 401
        assert (
            await c.get("/accounts", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await c.get("/accounts", headers={"Authorization": "Bearer s3cret"})
        ).status_code == 200
```

Append to `tests/test_cli.py`:

```python
def test_serve_refuses_public_bind_without_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    import uvicorn

    called: list[int] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(1))
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "token" in result.output.lower()
    assert not called


def test_serve_public_bind_allowed_with_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("MONETA_API_TOKEN", "t0k3n")
    import uvicorn

    called: list[int] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(1))
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0
    assert called


def test_in_process_cli_works_with_token_configured(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text('api_token = "t0k3n"\n')
    result = runner.invoke(app, ["networth"])  # client must attach the bearer header
    assert result.exit_code == 0
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement.**

`config.py` Settings: add `api_token: str | None = None`.

`api.py` — signature `def create_app(sessionmaker, adapter, llm, engine=None, api_token: str | None = None)`; add `Header` to the fastapi import; inside `create_app` before `app = FastAPI(...)`:

```python
    async def check_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if api_token is not None and authorization != f"Bearer {api_token}":
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    app = FastAPI(title="moneta", lifespan=lifespan, dependencies=[Depends(check_auth)])
```

`build_app`: pass `api_token=settings.api_token`.

`cli/client.py` `_arequest`: build `headers = {"Authorization": f"Bearer {settings.api_token}"} if settings.api_token else None` and pass `headers=headers` to `client.request(...)`.

`cli/main.py` `serve`:

```python
@app.command()
def serve(host: str = "127.0.0.1", port: int = 8300) -> None:
    """Run the moneta API server."""
    import uvicorn

    from moneta.config import load_settings

    if host not in ("127.0.0.1", "::1", "localhost") and not load_settings().api_token:
        console.print(
            "[red]Error:[/red] refusing to bind a non-loopback host without an API token. "
            "Set MONETA_API_TOKEN or api_token in config.toml."
        )
        raise typer.Exit(1)
    uvicorn.run("moneta.api:build_app", host=host, port=port, factory=True)
```

- [ ] **Step 4: Run full suite.**

- [ ] **Step 5: Verify gate + commit** (`feat: optional bearer-token auth; serve refuses public bind without it`)

---

### Task 12: `moneta backup` via VACUUM INTO

Manual state (account types, promos, review resolutions) can't be re-synced; there's no backup path.

**Files:**
- Modify: `src/moneta/api.py` (`POST /backup`), `src/moneta/cli/main.py` (`backup` command)
- Test: `tests/test_api.py`, `tests/test_cli.py`

**Interfaces:**
- `POST /backup` body `{"dest": str | null}` → `{"path": str}`. Default dest: `moneta-backup-<YYYYMMDD-HHMMSS>.db` next to the DB file. 400 if the app has no file-backed engine; 409 if dest exists. Path resolves server-side (documented in CLI help).

- [ ] **Step 1: Failing tests** (append to `tests/test_api.py`; imports: `Path` from pathlib, `init_db`/`make_sessionmaker` from moneta.db):

```python
async def test_backup_vacuum_into(tmp_path: Path) -> None:
    engine, sm = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    app = create_app(sm, adapter=None, llm=None, engine=engine)
    transport = httpx.ASGITransport(app=app)
    dest = tmp_path / "out.db"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/backup", json={"dest": str(dest)})
        assert r.status_code == 200
        assert r.json() == {"path": str(dest)}
        assert dest.exists() and dest.stat().st_size > 0
        assert (await c.post("/backup", json={"dest": str(dest)})).status_code == 409
    await engine.dispose()


async def test_backup_requires_file_backed_db(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(sessionmaker, adapter=None, llm=None)  # no engine → in-memory/unknown
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/backup", json={})).status_code == 400
```

- [ ] **Step 2: Run** — FAIL (404).

- [ ] **Step 3: Implement** in `api.py` (imports: `datetime` already added in Task 10; `Path` from pathlib):

```python
class BackupIn(BaseModel):
    dest: str | None = None
```

```python
    @app.post("/backup")
    async def backup(body: BackupIn) -> dict[str, str]:
        db_file = engine.url.database if engine is not None else None
        if not db_file or db_file == ":memory:":
            raise HTTPException(status_code=400, detail="backup requires a file-backed database")
        dest = (
            Path(body.dest).expanduser()
            if body.dest
            else Path(db_file).with_name(f"moneta-backup-{datetime.now():%Y%m%d-%H%M%S}.db")
        )
        if dest.exists():
            raise HTTPException(status_code=409, detail=f"destination already exists: {dest}")
        async with engine.connect() as conn:
            ac = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await ac.exec_driver_sql("VACUUM INTO ?", (str(dest),))
        return {"path": str(dest)}
```

CLI:

```python
@app.command()
def backup(
    dest: Annotated[str | None, typer.Argument(help="Destination file (server-side path).")] = None,
) -> None:
    """Snapshot the database with SQLite VACUUM INTO (safe while running)."""
    r = request("POST", "/backup", {"dest": dest} if dest else {})
    console.print(f"Backup written to [bold]{r['path']}[/bold]")
```

Add a CLI test to `tests/test_cli.py`:

```python
def test_backup_command(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    runner.invoke(app, ["networth"])  # forces DB creation
    dest = tmp_path / "snap.db"
    result = runner.invoke(app, ["backup", str(dest)])
    assert result.exit_code == 0
    assert dest.exists()
```

- [ ] **Step 4: Run full suite.**

- [ ] **Step 5: Verify gate + commit** (`feat: moneta backup — online SQLite snapshot via VACUUM INTO`)

---

### Task 13: Coverage batch

The single most valuable missing test (whole-sync idempotency) plus untested API/CLI branches.

**Files:**
- Test: `tests/test_run.py`, `tests/test_api.py`, `tests/test_cli.py`, `tests/test_config.py`

- [ ] **Step 1: Whole-pipeline idempotency** (append to `tests/test_run.py`; imports: `Decimal`, `func`, DTO types, `FakeAdapter`, models):

```python
async def test_second_identical_sync_is_a_full_noop(session: AsyncSession) -> None:
    snap = Snapshot(
        accounts=[
            AccountDTO(
                id="A-1", name="Checking", org_name="Chase", currency="USD",
                balance=Decimal("1000.00"), balance_date=date(2026, 7, 1),
            )
        ],
        transactions=[
            TransactionDTO(
                id=f"T-{i}", account_id="A-1", posted_on=posted,
                amount=Decimal("-15.99"), description="NETFLIX.COM", raw={},
            )
            for i, posted in enumerate(
                (date(2026, 4, 24), date(2026, 5, 24), date(2026, 6, 24))
            )
        ],
        holdings=[],
    )
    today = date(2026, 7, 9)
    await run_sync(session, FakeAdapter(snap), llm=None, today=today)

    async def counts() -> dict[str, int]:
        out: dict[str, int] = {}
        for model in (Account, Transaction, RecurringSeries, SeriesEvent, ReviewItem, TransferLink):
            out[model.__name__] = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        return out

    before = await counts()
    report = await run_sync(session, FakeAdapter(snap), llm=None, today=today)
    assert report.ingest.new_transactions == 0
    assert report.ingest.updated_transactions == 0
    assert report.recurring.new_series == 0
    assert report.transfers.linked == 0
    assert report.events == 0
    assert await counts() == before
```

Also assert domain atomicity in the existing failure test (Task 10 added it) — extend `test_run_sync_records_failure_and_reraises`:

```python
    txn_count = (
        await session.execute(select(func.count()).select_from(Transaction))
    ).scalar_one()
    assert txn_count == 0  # failed fetch leaves domain tables untouched
```

- [ ] **Step 2: API 404/422 branches** (append to `tests/test_api.py`):

```python
async def test_patch_unknown_account_is_404(client: httpx.AsyncClient) -> None:
    r = await client.patch("/accounts/999999", json={"type": "savings"})
    assert r.status_code == 404


async def test_resolve_unknown_review_item_is_404(client: httpx.AsyncClient) -> None:
    r = await client.post("/review/999999/resolve", json={"resolution": {}})
    assert r.status_code == 404


async def test_import_vesting_malformed_csv_is_422(client: httpx.AsyncClient) -> None:
    r = await client.post("/import/vesting", json={"csv": "ticker,vested\nACME,40\n"})
    assert r.status_code == 422
```

- [ ] **Step 3: CLI command coverage** (append to `tests/test_cli.py`):

```python
def test_obligations_renders_deferred_interest_warning(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed() -> None:
        from moneta.models import AccountType
        from tests.factories import make_account, make_series, make_txn

        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            loan = await make_account(
                session, type=AccountType.loan, name="Synchrony Financing",
                balance_cents=-120_000, promo_expires_on=date(2026, 9, 1),
            )
            checking = await make_account(session, type=AccountType.checking)
            series = await make_series(
                session, merchant="Synchrony", expected_cents=-10_000,
                next_expected_on=date(2026, 8, 1),
            )
            out = await make_txn(
                session, checking, amount_cents=-10_000,
                posted_on=date(2026, 7, 1), series_id=series.id,
            )
            inflow = await make_txn(
                session, loan, amount_cents=10_000, posted_on=date(2026, 7, 1)
            )
            from moneta.models import LinkMethod, TransferLink

            session.add(
                TransferLink(
                    outflow_id=out.id, inflow_id=inflow.id, confidence=1.0,
                    method=LinkMethod.manual,
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())
    result = runner.invoke(app, ["obligations"])
    assert result.exit_code == 0
    assert "Synchrony" in result.output
    assert "Traceback" not in result.output


def test_recurring_events_flag_renders_table(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed() -> None:
        from moneta.models import EventKind, SeriesEvent

        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            series = await make_series(session)
            session.add(
                SeriesEvent(
                    series_id=series.id, kind=EventKind.missed,
                    occurred_on=date(2026, 6, 15), details={"expected_on": "2026-06-15"},
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())
    result = runner.invoke(app, ["recurring", "--events"])
    assert result.exit_code == 0
    assert "missed" in result.output
    assert "Traceback" not in result.output


def test_setup_simplefin_saves_access_url(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def fake_claim(token: str) -> str:
        assert token == "TOKEN"
        return "https://u:p@bridge.example/simplefin"

    monkeypatch.setattr("moneta.aggregator.simplefin.claim_setup_token", fake_claim)
    result = runner.invoke(app, ["setup", "simplefin", "TOKEN"])
    assert result.exit_code == 0
    assert "connected" in result.output.lower()
    from moneta.config import load_settings

    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    assert load_settings().simplefin_access_url == "https://u:p@bridge.example/simplefin"
```

(If `obligations` seeding proves brittle against `compute_obligations` semantics, mirror the setup used in `tests/test_financing.py::test_deferred_interest_risk` instead — the goal is CLI rendering coverage, not financing logic.)

- [ ] **Step 4: Malformed config pin** (append to `tests/test_config.py`):

```python
def test_malformed_config_file_raises_toml_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text("not = = valid\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_settings()
```

(`import tomllib` at top. This pins current behavior so a future graceful-handling change is deliberate.)

- [ ] **Step 5: Run full suite. Verify gate + commit** (`test: whole-sync idempotency, failure atomicity, API 4xx branches, CLI coverage`)

---

### Task 14: Docs + backlog cleanup

**Files:**
- Modify: `docs/backlog/low/test-coverage-gaps.md` (remove stale LiteLLM bullet; remove any other bullets Tasks 5–13 covered), `CLAUDE.md`, `README.md`

- [ ] **Step 1:** In `docs/backlog/low/test-coverage-gaps.md`, delete the `LiteLLMClassifier error path` bullet (already covered by `tests/test_llm.py::test_provider_error_returns_none`). Also delete the `config.py env-precedence` bullet only if a test now covers it; otherwise leave.

- [ ] **Step 2:** Update `CLAUDE.md`:
- Conventions: add "Schema changes require an Alembic revision in `src/moneta/migrations/versions/` (`NNNN_name.py`, `down_revision` chained); `init_db` runs `upgrade head` and adopts pre-migration DBs by stamping `0001`. Never call `create_all` in production code."
- Pipeline notes: add "`run_sync` writes a `SyncRun` audit row (success or failure) before/after the stages; `emit_series_events` catches up all missed periods per sync and only accepts a price change confirmed by the two newest occurrences."
- Gotchas: add "`_ts_to_date` converts SimpleFIN timestamps in local time; tests pin `TZ=UTC` via an autouse fixture" and "views aggregate only the primary (majority) currency".

- [ ] **Step 3:** Update `README.md`: mention `moneta status`, `moneta backup`, and the optional `api_token` (required for non-loopback `moneta serve`).

- [ ] **Step 4: Verify gate + commit** (`docs: conventions for migrations, sync audit, currency/timezone semantics`)

---

## Post-implementation workflow (per global conventions)

1. Architect review of the full branch diff (subagent); fix every finding.
2. `/simplify` on the changed code; apply findings.
3. `/code-review` on the branch; fix confirmed findings.
4. QA-backlog subagent: file `docs/qa-backlog/` items for what automated tests can't verify (real-bridge timezone behavior, WAL under concurrent serve+CLI, real backup/restore, token auth from a remote client, migration adoption against the user's real DB).
5. CLAUDE.md improver skill.
6. PR to `main`.

## Self-Review Notes

- Spec coverage: all 14 review items map to Tasks 1–14 (item "stale backlog bullet" = Task 14; "logging + audit" = Task 10; "test additions" = Task 13 plus the per-task TDD tests).
- Type consistency: `advance_expected_on(d, cadence)` (Tasks 1–2), `IngestStats.updated_transactions` (Tasks 4, 13), `primary_currency` (Task 6), `create_app(..., api_token=...)` (Tasks 11–12 tests), `SyncRun`/`configure_logging` (Tasks 10, 13) — names match across tasks.
- Known judgment calls recorded in Interfaces blocks: two-sample price confirmation, majority-currency rule, server-side backup paths, adopt-by-stamp for pre-Alembic DBs.
