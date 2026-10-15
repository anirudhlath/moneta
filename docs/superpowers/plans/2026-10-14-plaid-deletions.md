# Plaid Deletions and Raw Account Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A transaction the bank withdraws disappears from moneta once Plaid has
finished loading that bank's history. A deleted transaction's id is never
reused, and every account keeps the provider's latest raw record.

**Architecture:**
- The Plaid adapter marks a replay complete only when its final page reports
  `HISTORICAL_UPDATE_COMPLETE`. It then puts each account's earliest posted date
  on the snapshot (`Snapshot.complete_replays`), and `concat_snapshots` merges
  those dates.
- `AccountDTO.raw` and `Account.raw` (migration `0008`) carry the provider's
  latest account record.
- `transactions` is rebuilt with SQLite `AUTOINCREMENT` (migration `0009`)
  before anything is deleted. A deleted row's id is never handed to a new row.
- `ingest_snapshot` deletes the stored rows of those Plaid accounts that are
  dated on or after that date and absent from the replay. Each deletion runs
  through the same pointer/link cleanup that upstream corrections already use.
- Reconciliation retires a price-change question whose transaction is gone.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 async + SQLite, Alembic,
pydantic, pytest (asyncio auto mode), uv.

**Spec:** No spec document. This plan implements GitHub issue #35 and the
Background section below. Read both first.

## Global Constraints

- A replay may delete rows only when all three of these hold:
  - its Plaid item finished: `has_more` is false **and**
    `transactions_update_status == "HISTORICAL_UPDATE_COMPLETE"`;
  - the rows are Plaid-sourced;
  - the rows have `posted_on ≥` the earliest date that replay returned for that account.
- An item that is `NOT_READY`, still paging, failed or skipped deletes nothing.
- Only the Plaid copy is deleted. A SimpleFIN duplicate of a deleted row becomes a
  root.
- `Account.raw` holds the latest observation only, with no history. The column
  is `JSON`, non-null, with `server_default "{}"`, added by `0008_account_raw.py`.
- Raw records never carry credentials. Plaid's record is its account object.
  SimpleFIN's record is its account JSON minus `transactions` and `holdings`.
- `transactions` uses `AUTOINCREMENT`, set up by
  `0009_transactions_autoincrement.py`. The rebuild keeps every id, constraint,
  server default and the column order. It lands before the deletion task.
  `_HEAD` (`src/moneta/db.py:16`) becomes `"0008"` in Task 2 and `"0009"` in
  Task 4.
- Money is integer cents; negative = outflow. Pipelines commit; views don't.
  Enum columns load as plain `str`: compare with `==`.
- The repo is **public**: use fictitious examples only (Acme Bank, Corner Curry,
  Elmton, Jane Doe, round amounts like $100.00 / `-12000` cents).
- No test in this plan hits an endpoint that reads `date.today()`. Any test you
  add that does must use date-relative data.
- Gate before every commit (output must be pristine; a warning is a failure):
  `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  During red/green steps run only the narrow test files named in the step.
- Docs upkeep happens inside the task that changes behavior, using the exact text
  given there.
- Commits use `git commit -s` with no AI co-author trailers. Work in the
  `fix-plaid-deletions` worktree (branch `fix-plaid-deletions`). Do not push.

## Review Focus

1. **An account whose complete replay returns no posted rows** (none at all, or
   only pending ones) has no earliest date. Nothing of it may be deleted. Pinned
   in Task 1 (`test_complete_replay_reports_each_accounts_earliest_posted_date`:
   a pending-only account gets no floor) and Task 5
   (`test_replay_without_rows_for_an_account_deletes_nothing`).
2. **A purchase the bank re-posts under a new id**, such as a pending charge that
   posts with a fresh id, loses the old row. The new row must still get the
   merchant a human gave it. Pinned in Task 5
   (`test_reissued_row_keeps_its_manual_merchant`).
3. **Two Plaid items in one sync**, one complete and one still loading or
   failing mid-paging. Only the complete item's accounts are swept. Pinned in
   Task 1 (`test_only_items_with_a_complete_replay_report_floors`) and Task 5
   (`test_replay_drops_reach_the_database_only_when_complete`).
4. **Running the same sync twice** must delete nothing the second time and report
   0. Pinned in Task 5 (`test_complete_replay_deletes_a_row_it_no_longer_returns`).
5. **A deleted Plaid row that a human had confirmed as the counterpart of a
   SimpleFIN row.** The full pipeline must count the SimpleFIN copy once, without
   crashing on the decision that now names a missing row. Pinned in Task 5
   (`test_simplefin_copy_is_counted_after_plaid_drops_its_own`).

## Background

### Defect 1: Plaid deletions are never removed

**Root cause** (each claim checked against the code):
- `PlaidAdapter._sync_pages` (`src/moneta/aggregator/plaid.py:296-333`) replays
  `/transactions/sync` from an empty cursor (`cursor = ""`, line 298) and reads
  only `added` (line 309). A replay from an empty cursor has no earlier cursor
  to diff against, so a missing id is the only sign that the bank withdrew a
  transaction.
- `has_more` (line 326) only drives paging. `transactions_update_status` is read
  only to log `NOT_READY` (lines 328-332). Neither value leaves the adapter.
- `ingest_snapshot` (`src/moneta/pipelines/ingest.py:82-121`) only adds new rows
  or rewrites corrected ones. No code path deletes a transaction, so a withdrawn
  charge stays in spending, transfers and series for good.

The claims are accurate, with one nuance: `has_more` is always false by the time
`_sync_pages` returns. A failure mid-paging raises, and then either the item is
skipped (`ITEM_ERROR`, `plaid.py:222-237`, which returns an empty snapshot) or
the whole sync fails. Either way "still paging" never reaches ingest, so the
only completeness signal needed is the final page's status.

**Owner's decision:** treat absence from a completed replay as deletion, within
the three limits in Global Constraints. The account → earliest-date map travels
on the `Snapshot` and is merged in `concat_snapshots`. Deletions go through the
correction cleanup (`ingest.py:123-149`), which clears `duplicate_of_id` pointers
and transfer links. By default only the Plaid copy is deleted. The owner also
asked for `transactions` to be rebuilt with `AUTOINCREMENT`, ahead of the
deletion work, so that rows are never deleted from a table that can reuse ids.

**What else a deletion touches** (audited):
- **`duplicate_of_id` pointers.** These would dangle, and `load_logical_data`
  raises on a dangling pointer (`src/moneta/logical.py:64-65`). SQLite foreign
  keys are off (`db.py` never sets the `foreign_keys` pragma), so the database
  would not stop it. Task 5 clears the pointers.
- **Transfer links on either leg.** Task 5 drops them.
- **Series tags.** These live on the row itself. A series left with no tagged
  row would stay active and keep emitting `missed` events, because detection
  never ends a series that has zero tagged transactions. Task 5 ends it, and
  gives a promoted copy the deleted root's tag.
- **Review items.**
  - Reconciliation already retires open merchant, transfer-pair and
    transaction-overlap questions when their rows vanish.
  - Resolved decisions are read tolerantly (`.get`, `in by_id`).
  - The gap is an open `price_change` question whose `transaction_id` is gone.
    Resolving it "yes" fails with a misleading error. Task 6 retires it.
- **`SeriesEvent` rows.** They reference series, not transactions, so they are
  untouched.
- **Ids.** `transactions` was created without `AUTOINCREMENT`
  (`0001_baseline.py`), so SQLite gives the next insert the highest deleted id.
  Review decisions name rows by id; a resolved transfer-pair decision has no
  other key. A reused id could therefore attach an old decision to an unrelated
  row. Task 4 rebuilds the table before Task 5 deletes anything.
  - **Prototype result.** Running the rebuild on a populated in-memory database:
    - kept every id, the `UNIQUE (account_id, aggregator_id)` constraint, all
      three foreign keys and the column order (the table has no explicit
      indexes and no server defaults);
    - left `sqlite_sequence` at the highest copied id.
  - **Caveat 1: reuse that already happened cannot be detected.** If an id was
    reused before the migration, the migration cannot tell. No code path
    deleted transactions before this branch, so earlier reuse could only come
    from hand edits to the database.
  - **Caveat 2: reflection loses `AUTOINCREMENT`.** SQLAlchemy reflection does
    not report it, so a later `batch_alter_table("transactions")` would quietly
    drop it. Task 4 adds a test and a `CLAUDE.md` note for that.

### Defect 2: raw Plaid account data isn't stored

**Root cause:**
- `AccountDTO` (`src/moneta/aggregator/base.py:12-21`) and `Account`
  (`src/moneta/models.py:156-173`) have no raw field, unlike
  `TransactionDTO.raw` / `Transaction.raw`.
- `_parse_account` (`plaid.py:181-203`) and `_parse_snapshot`
  (`src/moneta/aggregator/simplefin.py:129-171`) copy a handful of fields and
  drop the rest of the provider's record.
- Ingest (`ingest.py:38-62`) writes only the fields it is given.

The claims are accurate.

**Owner's decision:**
- Add `AccountDTO.raw`, defaulting to an empty dict.
- Add migration `0008`.
- Ingest writes the latest record on every sync.
- Plaid passes its account object. SimpleFIN passes its account JSON minus
  `transactions` and `holdings`.
- Anything credential-like is stripped.

**Credential check:**
- **Plaid.** The account object holds `account_id`, `balances`, `mask`, `name`,
  `official_name`, `type` and `subtype`, but no token. The access token travels
  only in the request body.
- **SimpleFIN.** The account (`org`, `id`, `name`, `currency`, `balance`,
  `available-balance`, `balance-date`, `extra`) holds no credentials either.
  `org.sfin-url` is the server's root URL, as the protocol defines it. The
  access URL's user and password live only in config (`_split_auth`).
- Tests in Task 3 pin that neither secret appears in a stored record.

## File Map

| File | Change | Task |
|---|---|---|
| `src/moneta/aggregator/base.py` | `Snapshot.complete_replays`, earliest-date merge in `concat_snapshots` | 1 |
| | `AccountDTO.raw` | 2 |
| `src/moneta/aggregator/plaid.py` | completeness flag + `_replay_floors` | 1 |
| | `raw=acct` | 3 |
| `src/moneta/models.py` | `Account.raw` | 2 |
| | `Transaction` gets `sqlite_autoincrement=True` | 4 |
| `src/moneta/migrations/versions/0008_account_raw.py` | create | 2 |
| `src/moneta/migrations/versions/0009_transactions_autoincrement.py` | create | 4 |
| `src/moneta/db.py` | `_HEAD = "0008"` | 2 |
| | `_HEAD = "0009"` | 4 |
| `src/moneta/pipelines/ingest.py` | writes `Account.raw` | 2 |
| | deletion sweep, `_void_relations`, `IngestStats.deleted_transactions` | 5 |
| `src/moneta/aggregator/simplefin.py` | raw record minus transactions/holdings | 3 |
| `src/moneta/cli/main.py` | `N transaction(s) removed upstream.` | 5 |
| `src/moneta/pipelines/reconcile.py` | retire price-change questions about vanished rows | 6 |
| `tests/test_plaid.py` | completeness, raw, end-to-end | 1, 3, 5 |
| `tests/test_run.py` | completeness, end-to-end | 1, 5 |
| `tests/test_migrations.py` | `0008` backfill | 2 |
| | `0009` rebuild, id reuse, downgrade | 4 |
| `tests/test_ingest.py` | raw | 2 |
| | sweep rules, cleanup | 5 |
| `tests/test_simplefin.py` | SimpleFIN raw | 3 |
| `tests/test_cli.py` | sync summary line | 5 |
| `tests/test_transaction_overlap.py` | price-change retirement | 6 |
| `CLAUDE.md`, `docs/PRD.md`, `docs/user-guide.md`, `README.md` | docs | 3, 4, 5, 6 |

---

### Task 1: Plaid reports complete replays

**Files:**
- Modify: `src/moneta/aggregator/base.py:40-61`
- Modify: `src/moneta/aggregator/plaid.py:161-162` (constants), `181-203` (add
  `_replay_floors` after `_parse_account`), `239-254`, `286-333`
- Test: `tests/test_plaid.py`, `tests/test_run.py`

**Interfaces:**
- Produces:
  - `Snapshot.complete_replays: dict[str, date]` maps an account's external id
    (the `AccountDTO.id`) to the earliest posted date in that account's complete
    replay. It defaults to `{}`.
  - `concat_snapshots` keeps the earliest date for each account.
  - `PlaidAdapter._sync_pages` and `_fetch_transactions` now return
    `tuple[list[TransactionDTO], bool]`. The bool is True when the history is
    complete.
  - `_replay_floors(txns: list[TransactionDTO]) -> dict[str, date]`.
  - `tests/test_plaid.py::_dated`, which Task 5 reuses.
- Docs: none. This is internal plumbing with no behavior change until Task 5
  consumes it.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_plaid.py`,
  directly after `test_not_ready_status_is_not_fatal`:

```python
def _dated(
    txn_id: str, day: date, account_id: str = "acc-chk", pending: bool = False
) -> dict[str, Any]:
    return {
        **_txn(txn_id, 12.0, pending=pending),
        "account_id": account_id,
        "date": day.isoformat(),
    }


async def test_complete_replay_reports_each_accounts_earliest_posted_date() -> None:
    pages = [
        httpx.Response(
            200,
            json=_sync_page(
                [
                    _dated("t1", date(2026, 6, 20)),
                    _dated("t2", date(2026, 6, 1), account_id="acc-card"),
                    # pending rows are never stored, so they never set a floor
                    _dated("t3", date(2026, 4, 1), pending=True),
                ],
                True,
            ),
        ),
        httpx.Response(
            200,
            json=_sync_page(
                [
                    _dated("t4", date(2026, 5, 3)),
                    _dated("t5", date(2026, 5, 9), account_id="acc-brok", pending=True),
                ],
                False,
            ),
        ),
    ]
    adapter = PlaidAdapter(_plaid_client(_accounts_then_sync(pages)), [_item(["transactions"])])
    snap = await adapter.fetch()
    # acc-brok returned only a pending row: no floor, so none of its rows can be judged
    assert snap.complete_replays == {"acc-chk": date(2026, 5, 3), "acc-card": date(2026, 6, 1)}


@pytest.mark.parametrize(
    "status",
    ["NOT_READY", "INITIAL_UPDATE_COMPLETE", "TRANSACTIONS_UPDATE_STATUS_UNKNOWN", None],
)
async def test_incomplete_history_reports_no_floors(status: str | None) -> None:
    page = _sync_page([_dated("t1", date(2026, 6, 1))], False)
    if status is None:
        del page["transactions_update_status"]
    else:
        page["transactions_update_status"] = status
    adapter = PlaidAdapter(
        _plaid_client(_accounts_then_sync([httpx.Response(200, json=page)])),
        [_item(["transactions"])],
    )
    snap = await adapter.fetch()
    assert [t.id for t in snap.transactions] == ["t1"]  # rows still arrive
    assert snap.complete_replays == {}


async def test_only_items_with_a_complete_replay_report_floors() -> None:
    def accounts(account_id: str) -> dict[str, Any]:
        account = {
            "account_id": account_id,
            "name": "Everyday Checking",
            "type": "depository",
            "subtype": "checking",
            "balances": {"current": 100.0},
        }
        return {"accounts": [account], "item": {"institution_name": "Acme Bank"}}

    june_1 = date(2026, 6, 1)
    pages = {
        "access-done": [
            httpx.Response(200, json=_sync_page([_dated("d1", june_1, "acc-done")], False))
        ],
        "access-wait": [
            httpx.Response(
                200,
                json=_sync_page([_dated("w1", june_1, "acc-wait")], False, status="NOT_READY"),
            )
        ],
        "access-dead": [
            httpx.Response(200, json=_sync_page([_dated("x1", june_1, "acc-dead")], True)),
            _plaid_error("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "re-link"),
        ],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["access_token"]
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json=accounts(token.replace("access-", "acc-")))
        return pages[token].pop(0)

    items = [
        PlaidItem(item_id=f"it-{name}", access_token=f"access-{name}", products=["transactions"])
        for name in ("done", "wait", "dead")
    ]
    snap = await PlaidAdapter(_plaid_client(handle), items).fetch()
    assert snap.complete_replays == {"acc-done": june_1}
    # the item that failed mid-paging is dropped whole; the loading one still syncs
    assert {a.id for a in snap.accounts} == {"acc-done", "acc-wait"}
    assert len(snap.warnings) == 1
```

  In `tests/test_run.py`, change the import
  `from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO` to
  `from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO, concat_snapshots`.
  Then append:

```python
def test_concat_snapshots_keeps_each_accounts_earliest_floor() -> None:
    def snap(floors: dict[str, date]) -> Snapshot:
        return Snapshot(accounts=[], transactions=[], holdings=[], complete_replays=floors)

    merged = concat_snapshots(
        [
            snap({"acc-a": date(2026, 6, 1)}),
            snap({"acc-a": date(2026, 5, 1), "acc-b": date(2026, 6, 9)}),
            snap({}),
        ]
    )
    assert merged.complete_replays == {"acc-a": date(2026, 5, 1), "acc-b": date(2026, 6, 9)}
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `uv run pytest -q tests/test_plaid.py tests/test_run.py`
Expected: the new tests FAIL with `AttributeError: 'Snapshot' object has no
attribute 'complete_replays'`. All existing tests pass.

- [ ] **Step 3: Implement.**

  In `src/moneta/aggregator/base.py`, add the field as the last line of
  `Snapshot` and replace `concat_snapshots`:

```python
class Snapshot(BaseModel):
    accounts: list[AccountDTO]
    transactions: list[TransactionDTO]
    holdings: list[HoldingDTO]
    warnings: list[str] = Field(default_factory=list)
    # account external id -> earliest posted date of a replay its provider called
    # complete. Ingest deletes that account's stored rows from that date on that
    # the replay no longer returned. Only the Plaid adapter fills it.
    complete_replays: dict[str, date] = Field(default_factory=dict)
```

```python
def concat_snapshots(snaps: list[Snapshot]) -> Snapshot:
    """Merge snapshots into one: the lists concatenate in order, and each account's
    replay floor is the earliest any snapshot reported."""
    floors: dict[str, date] = {}
    for s in snaps:
        for account_id, floor in s.complete_replays.items():
            floors[account_id] = min(floor, floors.get(account_id, floor))
    return Snapshot(
        accounts=[a for s in snaps for a in s.accounts],
        transactions=[t for s in snaps for t in s.transactions],
        holdings=[h for s in snaps for h in s.holdings],
        warnings=[w for s in snaps for w in s.warnings],
        complete_replays=floors,
    )
```

  In `src/moneta/aggregator/plaid.py`, add this constant directly below
  `_MUTATION_RETRIES = 3`:

```python
# the only status under which a replay is the bank's whole history (Plaid also
# reports NOT_READY, INITIAL_UPDATE_COMPLETE and TRANSACTIONS_UPDATE_STATUS_UNKNOWN)
_HISTORY_COMPLETE = "HISTORICAL_UPDATE_COMPLETE"
```

  Add this function directly after `_parse_account`:

```python
def _replay_floors(txns: list[TransactionDTO]) -> dict[str, date]:
    """Earliest posted date per account in a complete replay.

    Pending rows are never emitted, so an account that returned only pending (or no)
    transactions gets no floor, and ingest judges none of its rows.
    """
    floors: dict[str, date] = {}
    for t in txns:
        floors[t.account_id] = min(t.posted_on, floors.get(t.account_id, t.posted_on))
    return floors
```

  In `_fetch_item`, replace
  `snap.transactions = await self._fetch_transactions(item)` with:

```python
            snap.transactions, complete = await self._fetch_transactions(item)
            if complete:
                snap.complete_replays = _replay_floors(snap.transactions)
```

  Change the signatures of `_fetch_transactions` and `_sync_pages` to
  `-> tuple[list[TransactionDTO], bool]`. `_fetch_transactions` still does
  `return await self._sync_pages(item)` inside its retry loop. Replace the tail of
  `_sync_pages`, from `if data.get("transactions_update_status") == "NOT_READY":`
  through `return txns`, with:

```python
        status = data.get("transactions_update_status")
        if status == "NOT_READY":
            logger.info(
                "Plaid item {}: transaction history still preparing; next sync picks it up",
                item.institution_name or item.item_id,
            )
        # has_more is false here (the loop above only exits then); a replay is the
        # bank's whole history only once Plaid says the historical pull finished
        return txns, status == _HISTORY_COMPLETE
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_plaid.py tests/test_run.py`
Expected: PASS.

- [ ] **Step 5: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/aggregator/base.py src/moneta/aggregator/plaid.py tests/test_plaid.py tests/test_run.py
git commit -s -m "feat: Plaid reports each account's earliest date when a replay is complete"
```

---

### Task 2: Accounts can store a raw provider record

**Files:**
- Modify: `src/moneta/aggregator/base.py:12-21` (`AccountDTO`)
- Modify: `src/moneta/models.py:171-173` (append to `Account`)
- Create: `src/moneta/migrations/versions/0008_account_raw.py`
- Modify: `src/moneta/db.py:16`
- Modify: `src/moneta/pipelines/ingest.py:43-61` (account create/update)
- Test: `tests/test_migrations.py`, `tests/test_ingest.py`

**Interfaces:**
- Produces:
  - `AccountDTO.raw: dict[str, Any]`, defaulting to `{}`.
  - `Account.raw: Mapped[dict[str, Any]]`, the last column of `accounts`.
  - Ingest overwrites `Account.raw` with the DTO's `raw` on every sync.
  - In `tests/test_migrations.py`: `_alembic_config(conn: Connection) -> Config`,
    `_upgrade_to(conn: Connection, revision: str) -> None` and the SQL string
    `_INSERT_ACCOUNT`. Task 4 reuses them.
- Docs: none in this task. Nothing user-visible changes until the adapters fill
  `raw` in Task 3, which carries the docs.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_migrations.py`:
  - add `from alembic import command`;
  - change `from sqlalchemy import inspect` to `from sqlalchemy import inspect, select`;
  - change `from moneta.models import Base` to `from moneta.models import Account, Base`.

  Add these helpers after `_schema_of`:

```python
def _alembic_config(conn: Connection) -> Config:
    """A config that runs the real migrations on `conn` (env.py requires the connection)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(moneta.db.__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    return cfg


def _upgrade_to(conn: Connection, revision: str) -> None:
    """Stop at `revision`, unlike init_db, which always goes to head."""
    command.upgrade(_alembic_config(conn), revision)


# an account row with only the columns every revision since 0001 requires
_INSERT_ACCOUNT = (
    "INSERT INTO accounts (aggregator_id, name, org_name, type, currency, balance_cents,"
    " balance_date) VALUES ('ACT-1', 'Everyday Checking', 'Acme Bank', 'checking', 'USD',"
    " 10000, '2026-07-01')"
)
```

  Then append:

```python
async def test_0008_gives_existing_accounts_an_empty_raw() -> None:
    engine, sm = make_sessionmaker("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_to, "0007")
        await conn.exec_driver_sql(_INSERT_ACCOUNT)
    await init_db(engine)  # 0007 -> 0008
    async with sm() as session:
        account = (await session.scalars(select(Account))).one()
        assert account.raw == {}
    await engine.dispose()
```

  In `tests/test_ingest.py`, append:

```python
async def test_account_keeps_only_its_latest_raw_record(session: AsyncSession) -> None:
    def snap(seen: int) -> Snapshot:
        account = AccountDTO(
            id="plaid-raw",
            name="Everyday Checking",
            org_name="Acme Bank",
            currency="USD",
            balance=Decimal("100.00"),
            balance_date=date(2026, 7, 1),
            source="plaid",
            raw={"subtype": "checking", "seen": seen},
        )
        return Snapshot(accounts=[account], transactions=[], holdings=[])

    await ingest_snapshot(session, snap(1))
    account = (await session.scalars(select(Account))).one()
    assert account.raw == {"subtype": "checking", "seen": 1}
    await ingest_snapshot(session, snap(2))
    await session.refresh(account)
    assert account.raw == {"subtype": "checking", "seen": 2}
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `uv run pytest -q tests/test_migrations.py tests/test_ingest.py`
Expected: the two new tests FAIL (`Account` has no attribute `raw`).

- [ ] **Step 3: Implement.**

  `src/moneta/aggregator/base.py`: add as the last field of `AccountDTO`:

```python
    raw: dict[str, Any] = Field(default_factory=dict)  # the provider's account record
```

  `src/moneta/models.py`: add as the last column of `Account`, after
  `canonical_account_id`. It must be last, because
  `test_migrations_match_create_all_schema` compares column order.

```python
    # the provider's latest account record as fetched; latest observation only
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
```

  Create `src/moneta/migrations/versions/0008_account_raw.py`. It follows
  `0004`'s `add_column` and `0005`'s non-null JSON with a server default.

```python
"""Add accounts.raw (the provider's latest account record)."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("raw", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("accounts", "raw")
```

  `src/moneta/db.py:16`: `_HEAD = "0008"`.

  `src/moneta/pipelines/ingest.py`: in the `Account(...)` constructor, add
  `raw=dto.raw,` after `mask=dto.mask,`. In the `else:` branch, add
  `existing.raw = dto.raw` after `existing.mask = dto.mask`.

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_migrations.py tests/test_ingest.py`
Expected: PASS, including `test_migrations_match_create_all_schema` and
`test_head_constant_matches_script_directory`.

- [ ] **Step 5: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/aggregator/base.py src/moneta/models.py src/moneta/migrations/versions/0008_account_raw.py src/moneta/db.py src/moneta/pipelines/ingest.py tests/test_migrations.py tests/test_ingest.py
git commit -s -m "feat: accounts store the provider's latest raw record (migration 0008)"
```

---

### Task 3: Adapters pass their raw account records

**Files:**
- Modify: `src/moneta/aggregator/plaid.py:181-203` (`_parse_account`)
- Modify: `src/moneta/aggregator/simplefin.py:129-171` (`_parse_snapshot`, plus a
  module constant)
- Test: `tests/test_plaid.py`, `tests/test_simplefin.py`
- Docs: `docs/PRD.md`, `docs/user-guide.md`, `CLAUDE.md`. `README.md` is
  unchanged because no command or quickstart step moves.

**Interfaces:**
- Consumes `AccountDTO.raw` (Task 2).
- Produces:
  - A Plaid `AccountDTO.raw` is Plaid's account object, verbatim.
  - A SimpleFIN `AccountDTO.raw` is the bridge account minus `transactions` and
    `holdings`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_plaid.py`:

```python
async def test_accounts_keep_plaids_account_object_as_raw() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)

    snap = await PlaidAdapter(_plaid_client(handle), [_item()]).fetch()
    expected = json.loads(json.dumps(_ACCOUNTS_PAYLOAD["accounts"]))
    assert [a.raw for a in snap.accounts] == expected
    assert "access-1" not in json.dumps([a.raw for a in snap.accounts])  # no credentials
```

  Append to `tests/test_simplefin.py`:

```python
async def test_accounts_keep_the_bridge_record_without_transactions_as_raw() -> None:
    account = {
        "org": {
            "domain": "acme.example",
            "name": "Acme Bank",
            "sfin-url": "https://bridge.example/simplefin",
        },
        "id": "ACT-9",
        "name": "Everyday Checking",
        "currency": "USD",
        "balance": "100.00",
        "available-balance": "100.00",
        "balance-date": 1751328000,
        "transactions": [
            {
                "id": "TRN-9",
                "posted": 1751328000,
                "amount": "-12.00",
                "description": "CORNER CURRY ELMTON",
            }
        ],
        "holdings": [],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [], "accounts": [account]})

    adapter = SimpleFINAdapter(
        "https://jane:s3cret-token@bridge.example/simplefin",
        client=_mock_client(httpx.MockTransport(handle)),
    )
    snap = await adapter.fetch()
    expected = {k: v for k, v in account.items() if k not in ("transactions", "holdings")}
    assert snap.accounts[0].raw == expected
    assert "s3cret-token" not in json.dumps(snap.accounts[0].raw)  # no credentials
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `uv run pytest -q tests/test_plaid.py tests/test_simplefin.py`
Expected: both new tests FAIL (`raw` is `{}`).

- [ ] **Step 3: Implement.**

  `src/moneta/aggregator/plaid.py`, `_parse_account`: add `raw=acct,` after
  `mask=acct.get("mask"),`. Plaid's account object holds no credentials; the
  access token travels only in the request body.

  `src/moneta/aggregator/simplefin.py`: add below `_TIMEOUT`:

```python
# stored as their own rows; the rest of a bridge account is its raw record (it holds no
# credentials: org.sfin-url is the server root, the access URL's auth lives in config)
_NOT_ACCOUNT_FIELDS = frozenset({"transactions", "holdings"})
```

  In `_parse_snapshot`'s `AccountDTO(...)`, add after `mask=infer_mask(acct["name"]),`:

```python
                raw={k: v for k, v in acct.items() if k not in _NOT_ACCOUNT_FIELDS},
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_plaid.py tests/test_simplefin.py tests/test_ingest.py`
Expected: PASS.

- [ ] **Step 5: Update the docs.**

  `docs/PRD.md`, §6.1 Plaid row. Replace
  ``Supplies real account types (`type_hint`) that beat keyword inference.``
  with:

```markdown
Supplies real account types (`type_hint`) that beat keyword inference, and each account's latest Plaid account object is kept in `Account.raw` (migration `0008`).
```

  `docs/PRD.md`, §6.1 SimpleFIN row. Replace
  `Each bridge request waits up to 60 s, because live institution refreshes routinely outlast httpx's 5 s default. |`
  with:

```markdown
Each bridge request waits up to 60 s, because live institution refreshes routinely outlast httpx's 5 s default. Each account's latest bridge record, minus its transactions and holdings, is kept in `Account.raw`. |
```

  `docs/PRD.md`, §7 feature history. Replace
  `| Date | Release | What shipped |` + newline + `|---|---|---|` with:

```markdown
| Date | Release | What shipped |
|---|---|---|
| 2026-10-15 | **Raw account records** | Each account keeps its provider's latest record in `Account.raw` (migration `0008`): Plaid's account object, or the SimpleFIN bridge account minus its transactions and holdings. Only the newest observation is kept, and neither record carries credentials. |
```

  `docs/user-guide.md`, files in the config dir. Replace
  ``- `moneta.db` — the database``
  with
  ``- `moneta.db` — the database (it also keeps each account's latest raw provider record)``.

  `CLAUDE.md`, Layout. Replace
  ``no merging layer; DTOs stop at `pipelines/ingest.py` ``
  with:

```markdown
no merging layer; DTOs stop at `pipelines/ingest.py`, which keeps each account's latest provider record in `Account.raw` (Plaid's account object; SimpleFIN's account minus `transactions`/`holdings`; latest observation only)
```

- [ ] **Step 6: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/aggregator/plaid.py src/moneta/aggregator/simplefin.py tests/test_plaid.py tests/test_simplefin.py docs/PRD.md docs/user-guide.md CLAUDE.md
git commit -s -m "feat: Plaid and SimpleFIN pass their raw account records (#35)"
```

---

### Task 4: Transaction ids are never reused

**Files:**
- Modify: `src/moneta/models.py:178` (`Transaction.__table_args__`)
- Create: `src/moneta/migrations/versions/0009_transactions_autoincrement.py`
- Modify: `src/moneta/db.py:16`
- Test: `tests/test_migrations.py`
- Docs: `CLAUDE.md`, `docs/PRD.md`. The user guide and `README.md` are
  unchanged, because nothing a user runs or sees changes.

**Interfaces:**
- Consumes `_upgrade_to`, `_alembic_config` and `_INSERT_ACCOUNT` from
  `tests/test_migrations.py` (Task 2).
- Produces:
  - `transactions` is `INTEGER PRIMARY KEY AUTOINCREMENT` on both the migration
    path and `Base.metadata.create_all`.
  - `sqlite_sequence` holds the highest id ever assigned, so a new id is always
    above every existing or deleted id.
  - `_HEAD = "0009"`.
  - Task 5 relies on this before it deletes any row.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_migrations.py`:
  - change `from sqlalchemy.ext.asyncio import AsyncEngine` to
    `from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession`;
  - change `from tests.factories import make_series` to
    `from tests.factories import make_account, make_series, make_txn`.

  Add these helpers after `_INSERT_ACCOUNT`:

```python
def _downgrade_to(conn: Connection, revision: str) -> None:
    command.downgrade(_alembic_config(conn), revision)


def _autoincrement_tables(conn: Connection) -> set[str]:
    rows = conn.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'table'").all()
    return {name for name, sql in rows if "AUTOINCREMENT" in (sql or "")}


def _constraints(conn: Connection) -> tuple[object, ...]:
    """What a table rebuild must keep: constraints, indexes, column order and defaults."""
    insp = inspect(conn)
    return (
        insp.get_unique_constraints("transactions"),
        sorted(insp.get_foreign_keys("transactions"), key=lambda fk: fk["constrained_columns"]),
        insp.get_indexes("transactions"),
        [(c["name"], c["default"]) for c in insp.get_columns("transactions")],
    )
```

  Then append:

```python
async def test_transactions_autoincrement_matches_the_models() -> None:
    via_models, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with via_models.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    via_migrations, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(via_migrations)
    for engine in (via_models, via_migrations):
        async with engine.connect() as conn:
            assert await conn.run_sync(_autoincrement_tables) == {"transactions"}
        await engine.dispose()


async def test_a_deleted_transaction_id_is_never_reused(session: AsyncSession) -> None:
    account = await make_account(session)
    await make_txn(session, account)
    newest = await make_txn(session, account)
    deleted_id = newest.id
    await session.delete(newest)
    await session.flush()
    again = await make_txn(session, account)
    assert again.id > deleted_id  # without AUTOINCREMENT SQLite hands out deleted_id again


async def test_0009_keeps_rows_ids_and_constraints() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_to, "0007")
        await conn.exec_driver_sql(_INSERT_ACCOUNT)
        for txn_id, duplicate_of in ((3, None), (7, 3), (12, None)):
            await conn.exec_driver_sql(
                "INSERT INTO transactions (id, account_id, aggregator_id, posted_on,"
                " amount_cents, description, raw, duplicate_of_id)"
                " VALUES (?, 1, ?, '2026-07-01', -12000, 'CORNER CURRY ELMTON', '{}', ?)",
                (txn_id, f"TRN-{txn_id}", duplicate_of),
            )
        before = await conn.run_sync(_constraints)
    await init_db(engine)  # 0007 -> 0008 -> 0009
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT id, aggregator_id, duplicate_of_id FROM transactions ORDER BY id"
        )
        assert [tuple(r) for r in rows] == [
            (3, "TRN-3", None),
            (7, "TRN-7", 3),
            (12, "TRN-12", None),
        ]
        assert await conn.run_sync(_constraints) == before
        seq = await conn.exec_driver_sql(
            "SELECT seq FROM sqlite_sequence WHERE name = 'transactions'"
        )
        assert seq.scalar_one() == 12  # new ids land above every id the table holds
    await engine.dispose()


async def test_0009_downgrade_restores_the_plain_table() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_INSERT_ACCOUNT)
        await conn.exec_driver_sql(
            "INSERT INTO transactions (account_id, aggregator_id, posted_on, amount_cents,"
            " description, raw)"
            " VALUES (1, 'TRN-1', '2026-07-01', -12000, 'CORNER CURRY ELMTON', '{}')"
        )
        await conn.run_sync(_downgrade_to, "0008")
        assert await conn.run_sync(_autoincrement_tables) == set()
        ids = await conn.exec_driver_sql("SELECT id FROM transactions")
        assert list(ids.scalars()) == [1]
    await engine.dispose()
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `uv run pytest -q tests/test_migrations.py`
Expected:
- `test_transactions_autoincrement_matches_the_models` FAILS (`set() != {'transactions'}`).
- `test_a_deleted_transaction_id_is_never_reused` FAILS: the new id equals
  `deleted_id`.
- `test_0009_keeps_rows_ids_and_constraints` FAILS with
  `no such table: sqlite_sequence`.
- `test_0009_downgrade_restores_the_plain_table` already PASSES, because there is
  no 0009 to undo yet. It pins the downgrade from Step 3 on.

- [ ] **Step 3: Implement.**

  `src/moneta/models.py`, `Transaction`: replace
  `__table_args__ = (UniqueConstraint("account_id", "aggregator_id"),)` with:

```python
    # AUTOINCREMENT (migration 0009): ingest deletes rows a complete Plaid replay
    # dropped, and review payloads name rows by id, so an id must never come back
    __table_args__ = (
        UniqueConstraint("account_id", "aggregator_id"),
        {"sqlite_autoincrement": True},
    )
```

  Create `src/moneta/migrations/versions/0009_transactions_autoincrement.py`.
  It follows `0007`'s `recreate="always"` batch rebuild. Reflection carries the
  columns in order, the unnamed `UNIQUE (account_id, aggregator_id)` and the
  three foreign keys; the table has no explicit indexes or server defaults.

```python
"""Never reuse a transaction id: rebuild transactions with AUTOINCREMENT.

Without AUTOINCREMENT SQLite hands the highest deleted id to the next insert, and
review payloads name transactions by id. The batch copy keeps every id, and SQLite
sets sqlite_sequence to the highest id copied, so new ids land above every id in the
table. An id reused before this migration cannot be detected.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot add AUTOINCREMENT in place. Reflection does not report it either,
    # so any later batch rebuild of this table must pass the same table_kwargs.
    with op.batch_alter_table(
        "transactions", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ):
        pass


def downgrade() -> None:
    with op.batch_alter_table(
        "transactions", recreate="always", table_kwargs={"sqlite_autoincrement": False}
    ):
        pass
```

  `src/moneta/db.py:16`: `_HEAD = "0009"`.

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_migrations.py tests/test_ingest.py tests/test_transaction_overlap.py`
Expected: PASS, including `test_migrations_match_create_all_schema`,
`test_init_db_adopts_pre_migration_database` and
`test_head_constant_matches_script_directory`.

- [ ] **Step 5: Update the docs.**

  `CLAUDE.md`, schema-changes bullet. Replace
  `so forgetting either fails the suite.`
  with:

```markdown
so forgetting either fails the suite. `transactions` is `AUTOINCREMENT` (migration `0009`, model `sqlite_autoincrement=True`) so a deleted row's id is never handed out again — review payloads and transfer links name rows by id. Reflection does not report `AUTOINCREMENT`: any later `batch_alter_table("transactions", ...)` must pass `table_kwargs={"sqlite_autoincrement": True}` or it silently drops it (`test_transactions_autoincrement_matches_the_models` catches that).
```

  `docs/PRD.md`, §7 feature history. Replace
  `| Date | Release | What shipped |` + newline + `|---|---|---|` with:

```markdown
| Date | Release | What shipped |
|---|---|---|
| 2026-10-15 | **Transaction ids are never reused** | `transactions` is rebuilt with SQLite `AUTOINCREMENT` (migration `0009`): every row keeps its id and new ids start above the highest one, so a deleted transaction's id never comes back as a different transaction that a review decision would then misread. |
```

- [ ] **Step 6: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/models.py src/moneta/migrations/versions/0009_transactions_autoincrement.py src/moneta/db.py tests/test_migrations.py CLAUDE.md docs/PRD.md
git commit -s -m "feat: transaction ids are never reused (AUTOINCREMENT, migration 0009)"
```

---

### Task 5: Ingest deletes what a complete replay dropped

**Files:**
- Modify: `src/moneta/pipelines/ingest.py:1-8` (imports), `27-31`
  (`IngestStats`), `123-149` (cleanup), plus new helpers at the end of the file
- Modify: `src/moneta/cli/main.py:157-160`
- Test: `tests/test_ingest.py`, `tests/test_plaid.py`, `tests/test_run.py`,
  `tests/test_cli.py`
- Docs: `CLAUDE.md`, `docs/PRD.md`, `docs/user-guide.md`, `README.md`

**Interfaces:**
- Consumes:
  - `Snapshot.complete_replays: dict[str, date]` (Task 1);
  - `tests/test_plaid.py::_dated` (Task 1);
  - `transactions` AUTOINCREMENT (Task 4), so a deleted id is never reissued.
- Produces:
  - `IngestStats.deleted_transactions: int`, default 0. It surfaces in
    `SyncReport.ingest` and the `/sync` JSON.
  - `ingest._void_relations(session: AsyncSession, ids: set[int]) -> None`.
  - `ingest._absent_from_replay(snap, acct_ids, stored) -> set[int]`.
  - `ingest._delete_absent(session: AsyncSession, gone: set[int]) -> None`.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_ingest.py`, replace the import block with:

```python
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AccountDTO, HoldingDTO, Snapshot, TransactionDTO
from moneta.logical import load_logical_data
from moneta.models import (
    Account,
    AccountType,
    AliasSource,
    Holding,
    LinkMethod,
    MerchantAlias,
    SeriesStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.ingest import infer_account_type, ingest_snapshot
from moneta.pipelines.normalize import normalize_merchants
from tests.factories import make_account, make_series, make_txn
```

  Then append:

```python
_ACME = AccountDTO(
    id="plaid-acme",
    name="Everyday Checking",
    org_name="Acme Bank",
    currency="USD",
    balance=Decimal("100.00"),
    balance_date=date(2026, 7, 1),
    source="plaid",
)
JUNE_1, JUNE_15 = date(2026, 6, 1), date(2026, 6, 15)


def _row(
    txn_id: str, posted_on: date, description: str = "CORNER CURRY ELMTON"
) -> TransactionDTO:
    """A $120.00 purchase on the Acme Plaid account."""
    return TransactionDTO(
        id=txn_id,
        account_id=_ACME.id,
        posted_on=posted_on,
        amount=Decimal("-120.00"),
        description=description,
        raw={},
    )


def _replay(*rows: TransactionDTO, complete: bool = True) -> Snapshot:
    """What the Plaid adapter hands ingest: a complete replay floors at its earliest row."""
    floors = {_ACME.id: min(r.posted_on for r in rows)} if complete and rows else {}
    return Snapshot(
        accounts=[_ACME], transactions=list(rows), holdings=[], complete_replays=floors
    )


async def _stored_ids(session: AsyncSession) -> set[str]:
    return set(await session.scalars(select(Transaction.aggregator_id)))


async def _acme_account(session: AsyncSession) -> Account:
    """The stored Acme Plaid account, for tests that seed rows through the factories."""
    return await make_account(
        session, aggregator_id=_ACME.id, name=_ACME.name, org_name=_ACME.org_name, source="plaid"
    )


async def _seed(
    session: AsyncSession, account: Account, aggregator_id: str, posted_on: date, **kw: Any
) -> Transaction:
    """A stored $120.00 Corner Curry row, as an earlier sync would have left it."""
    fields: dict[str, Any] = {"amount_cents": -12000, "description": "CORNER CURRY ELMTON", **kw}
    return await make_txn(
        session, account, aggregator_id=aggregator_id, posted_on=posted_on, **fields
    )


async def test_complete_replay_deletes_a_row_it_no_longer_returns(session: AsyncSession) -> None:
    await ingest_snapshot(session, _replay(_row("t1", JUNE_1), _row("t2", JUNE_15)))
    stats = await ingest_snapshot(session, _replay(_row("t1", JUNE_1)))
    assert stats.deleted_transactions == 1
    assert await _stored_ids(session) == {"t1"}
    # the same replay again finds nothing more to delete
    again = await ingest_snapshot(session, _replay(_row("t1", JUNE_1)))
    assert again.deleted_transactions == 0
    assert await _stored_ids(session) == {"t1"}


async def test_incomplete_replay_deletes_nothing(session: AsyncSession) -> None:
    await ingest_snapshot(session, _replay(_row("t1", JUNE_1), _row("t2", JUNE_15)))
    stats = await ingest_snapshot(session, _replay(_row("t1", JUNE_1), complete=False))
    assert stats.deleted_transactions == 0
    assert await _stored_ids(session) == {"t1", "t2"}


async def test_replay_without_rows_for_an_account_deletes_nothing(session: AsyncSession) -> None:
    await ingest_snapshot(session, _replay(_row("t1", JUNE_1)))
    stats = await ingest_snapshot(session, _replay())  # complete, but no earliest date
    assert stats.deleted_transactions == 0
    assert await _stored_ids(session) == {"t1"}


async def test_rows_older_than_the_replays_earliest_date_survive(session: AsyncSession) -> None:
    old = _row("t0", date(2024, 9, 1))
    await ingest_snapshot(
        session,
        _replay(old, _row("t1", JUNE_1), _row("t1b", JUNE_1), _row("t2", JUNE_15)),
    )
    # t0 aged out of the replay window; t1b sits on the earliest date and is gone
    stats = await ingest_snapshot(session, _replay(_row("t1", JUNE_1)))
    assert stats.deleted_transactions == 2
    assert await _stored_ids(session) == {"t0", "t1"}


async def test_floor_on_a_non_plaid_account_deletes_nothing(session: AsyncSession) -> None:
    other = _ACME.model_copy(update={"source": "simplefin"})
    rows = [_row("t1", JUNE_1), _row("t2", JUNE_15)]
    await ingest_snapshot(session, Snapshot(accounts=[other], transactions=rows, holdings=[]))
    stats = await ingest_snapshot(
        session,
        Snapshot(
            accounts=[other],
            transactions=rows[:1],
            holdings=[],
            complete_replays={other.id: JUNE_1},
        ),
    )
    assert stats.deleted_transactions == 0
    assert await _stored_ids(session) == {"t1", "t2"}


async def test_only_accounts_with_a_floor_are_swept(session: AsyncSession) -> None:
    savings = _ACME.model_copy(update={"id": "plaid-acme-savings", "name": "Savings"})
    s1 = TransactionDTO(
        id="s1",
        account_id=savings.id,
        posted_on=JUNE_15,
        amount=Decimal("-12.00"),
        description="ELMTON WATER",
        raw={},
    )
    await ingest_snapshot(
        session,
        Snapshot(accounts=[_ACME, savings], transactions=[_row("t1", JUNE_1), s1], holdings=[]),
    )
    # savings returned nothing this time and has no floor, so its row is not judged
    second = _replay(_row("t1", JUNE_1))
    second.accounts.append(savings)
    stats = await ingest_snapshot(session, second)
    assert stats.deleted_transactions == 0
    assert await _stored_ids(session) == {"t1", "s1"}


async def test_deleted_root_promotes_its_other_provider_copy(session: AsyncSession) -> None:
    plaid = await _acme_account(session)
    simplefin = await make_account(session, source="simplefin", canonical_account_id=plaid.id)
    series = await make_series(session, merchant="Corner Curry", expected_cents=-12000)
    await _seed(session, plaid, "t1", JUNE_1)
    root = await _seed(session, plaid, "t2", JUNE_15, series_id=series.id)
    copy = await _seed(session, simplefin, "sf-2", JUNE_15, duplicate_of_id=root.id)
    stats = await ingest_snapshot(session, _replay(_row("t1", JUNE_1)))
    assert stats.deleted_transactions == 1
    await session.refresh(copy)
    await session.refresh(series)
    assert copy.duplicate_of_id is None
    assert copy.series_id == series.id  # the bill keeps its evidence
    assert series.status == SeriesStatus.active
    # nothing dangles: the logical projection loads and counts the copy
    logical = await load_logical_data(session)
    assert copy.id in {t.id for t in logical.transactions}


async def test_deleting_a_transfer_leg_drops_its_link(session: AsyncSession) -> None:
    plaid = await _acme_account(session)
    savings = await make_account(session, type=AccountType.savings)
    await _seed(session, plaid, "t1", JUNE_1)
    out = await _seed(session, plaid, "t2", JUNE_15, amount_cents=-10000, description="TRANSFER")
    inn = await make_txn(session, savings, posted_on=JUNE_15, amount_cents=10000)
    session.add(
        TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method=LinkMethod.rule)
    )
    stats = await ingest_snapshot(session, _replay(_row("t1", JUNE_1)))
    assert stats.deleted_transactions == 1
    assert list(await session.scalars(select(TransferLink))) == []
    assert await session.get(Transaction, inn.id) is not None  # the other leg stays


async def test_series_left_without_a_tagged_row_ends(session: AsyncSession) -> None:
    plaid = await _acme_account(session)
    orphan = await make_series(session, merchant="Corner Curry", expected_cents=-12000)
    supported = await make_series(session, merchant="Acme Fitness", expected_cents=-12000)
    fitness = "ACME FITNESS"
    await _seed(session, plaid, "keep", JUNE_1, description=fitness, series_id=supported.id)
    await _seed(session, plaid, "gone", JUNE_15, series_id=orphan.id)
    await _seed(session, plaid, "gone2", JUNE_15, description=fitness, series_id=supported.id)
    stats = await ingest_snapshot(session, _replay(_row("keep", JUNE_1, fitness)))
    assert stats.deleted_transactions == 2
    await session.refresh(orphan)
    await session.refresh(supported)
    assert orphan.status == SeriesStatus.ended  # its only evidence is gone
    assert supported.status == SeriesStatus.active  # "keep" still carries it


async def test_reissued_row_keeps_its_manual_merchant(session: AsyncSession) -> None:
    session.add(
        MerchantAlias(
            raw_descriptor="CORNER CURRY 0042", merchant="Corner Curry", source=AliasSource.manual
        )
    )
    await ingest_snapshot(session, _replay(_row("old-id", JUNE_1, "CORNER CURRY 0042")))
    await normalize_merchants(session, None)
    # the bank withdraws the row and re-posts the same purchase under a new id
    stats = await ingest_snapshot(session, _replay(_row("new-id", JUNE_1, "CORNER CURRY 0042")))
    await normalize_merchants(session, None)
    assert stats.deleted_transactions == 1
    rows = list(await session.scalars(select(Transaction)))
    assert [(t.aggregator_id, t.merchant) for t in rows] == [("new-id", "Corner Curry")]
```

  In `tests/test_plaid.py`, add these imports (ruff orders them):
  `from sqlalchemy import select`,
  `from sqlalchemy.ext.asyncio import AsyncSession`,
  `from moneta.pipelines.ingest import ingest_snapshot`. Also change
  `from moneta.models import AccountType` to
  `from moneta.models import AccountType, Transaction`. Then append, after
  `test_only_items_with_a_complete_replay_report_floors`:

```python
@pytest.mark.parametrize(
    ("second_sync", "expected"),
    [("complete", {"t1"}), ("not_ready", {"t1", "t2"}), ("failed_mid_paging", {"t1", "t2"})],
)
async def test_replay_drops_reach_the_database_only_when_complete(
    session: AsyncSession, second_sync: str, expected: set[str]
) -> None:
    june_1, june_15 = date(2026, 6, 1), date(2026, 6, 15)
    first = [
        httpx.Response(
            200, json=_sync_page([_dated("t1", june_1), _dated("t2", june_15)], False)
        )
    ]
    second = {
        "complete": [httpx.Response(200, json=_sync_page([_dated("t1", june_1)], False))],
        "not_ready": [
            httpx.Response(
                200, json=_sync_page([_dated("t1", june_1)], False, status="NOT_READY")
            )
        ],
        "failed_mid_paging": [
            httpx.Response(200, json=_sync_page([_dated("t1", june_1)], True)),
            _plaid_error("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "re-link"),
        ],
    }[second_sync]
    for pages in (first, second):
        adapter = PlaidAdapter(
            _plaid_client(_accounts_then_sync(pages)), [_item(["transactions"])]
        )
        await ingest_snapshot(session, await adapter.fetch())
    assert set(await session.scalars(select(Transaction.aggregator_id))) == expected
```

  In `tests/test_run.py`, add the imports
  `from moneta.logical import load_logical_data` and
  `from moneta.pipelines.transaction_overlap import set_transaction_duplicate`.
  Then append:

```python
def _acme_plaid() -> AccountDTO:
    return AccountDTO(
        id="plaid-acme",
        name="Everyday Checking",
        org_name="Acme Bank",
        currency="USD",
        balance=Decimal("100.00"),
        balance_date=date(2026, 7, 9),
        source="plaid",
    )


def _curry(txn_id: str, posted_on: date) -> TransactionDTO:
    return TransactionDTO(
        id=txn_id,
        account_id="plaid-acme",
        posted_on=posted_on,
        amount=Decimal("-12.00"),
        description="CORNER CURRY ELMTON",
        raw={},
    )


async def test_sync_report_counts_rows_a_complete_replay_dropped(session: AsyncSession) -> None:
    t1, t2 = _curry("t1", date(2026, 6, 1)), _curry("t2", date(2026, 6, 15))
    adapter = FakeAdapter(
        Snapshot(accounts=[_acme_plaid()], transactions=[t1, t2], holdings=[]), source="plaid"
    )
    await run_sync(session, [adapter], llm=None, today=date(2026, 7, 9))
    adapter.snapshot = Snapshot(
        accounts=[_acme_plaid()],
        transactions=[t1],
        holdings=[],
        complete_replays={"plaid-acme": t1.posted_on},
    )
    report = await run_sync(session, [adapter], llm=None, today=date(2026, 7, 9))
    assert report.ingest.deleted_transactions == 1
    runs = list(await session.scalars(select(SyncRun).order_by(SyncRun.id)))
    assert runs[-1].report is not None
    assert runs[-1].report["ingest"]["deleted_transactions"] == 1


async def test_simplefin_copy_is_counted_after_plaid_drops_its_own(session: AsyncSession) -> None:
    plaid = await make_account(
        session, aggregator_id="plaid-acme", name="Everyday Checking", source="plaid"
    )
    simplefin = await make_account(session, source="simplefin", canonical_account_id=plaid.id)
    await make_txn(
        session,
        plaid,
        aggregator_id="keep",
        posted_on=date(2026, 6, 1),
        amount_cents=-1200,
        description="CORNER CURRY ELMTON",
    )
    gone = await make_txn(
        session,
        plaid,
        aggregator_id="gone",
        posted_on=date(2026, 6, 15),
        amount_cents=-4500,
        description="ELMTON GROCER",
    )
    copy = await make_txn(
        session,
        simplefin,
        aggregator_id="s1",
        posted_on=date(2026, 6, 15),
        amount_cents=-4500,
        description="ELMTON GROCER",
    )
    # a human confirmed the SimpleFIN row duplicates the Plaid one
    await set_transaction_duplicate(session, copy.id, gone.id)
    await session.commit()
    replay = Snapshot(
        accounts=[_acme_plaid()],
        transactions=[_curry("keep", date(2026, 6, 1))],
        holdings=[],
        complete_replays={"plaid-acme": date(2026, 6, 1)},
    )
    report = await run_sync(
        session, [FakeAdapter(replay, source="plaid")], llm=None, today=date(2026, 7, 9)
    )
    assert report.ingest.deleted_transactions == 1
    logical = await load_logical_data(session)
    assert {t.aggregator_id for t in logical.transactions} == {"keep", "s1"}
```

  In `tests/test_cli.py`, append after `test_sync_omits_warnings_section_when_healthy`:

```python
@pytest.mark.parametrize("deleted", [2, 0])
def test_sync_reports_rows_removed_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deleted: int
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
                "ingest": {"new_transactions": 0, "deleted_transactions": deleted},
                "transfers": {"linked": 0},
                "recurring": {"new_series": 0},
                "events": 0,
                "auto_resolved": 0,
                "verify": {"verified": 0, "flagged": 0},
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert ("2 transaction(s) removed upstream." in result.output) is (deleted == 2)
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `uv run pytest -q tests/test_ingest.py tests/test_plaid.py tests/test_run.py tests/test_cli.py`
Expected:
- Every new `test_ingest.py` test FAILS: either with `AttributeError` on
  `deleted_transactions`, or because the rows, link or series are unchanged.
- In `test_replay_drops_reach_the_database_only_when_complete`, `[complete]`
  FAILS (`{"t1", "t2"}`).
- `test_sync_report_counts_rows_a_complete_replay_dropped` and
  `test_simplefin_copy_is_counted_after_plaid_drops_its_own` FAIL.
- In `test_sync_reports_rows_removed_upstream`, `[2]` FAILS.
- These already PASS, because they pin that nothing over-deletes: `[not_ready]`,
  `[failed_mid_paging]` and `[0]`.

- [ ] **Step 3: Implement.**

  In `src/moneta/pipelines/ingest.py`, replace the imports on lines 4 and 8:

```python
from sqlalchemy import delete, or_, select, update
```

```python
from moneta.models import (
    Account,
    AccountType,
    Holding,
    RecurringSeries,
    SeriesStatus,
    Transaction,
    TransferLink,
    to_cents,
)
```

  Add the counter as the last field of `IngestStats`:

```python
    deleted_transactions: int = 0
```

  Replace the whole `if relinked_ids:` block (lines 123-149, through
  `await session.delete(link)`) with:

```python
    # link_transfers and overlap reconciliation re-match on the corrected values
    await _void_relations(session, relinked_ids)
    gone = _absent_from_replay(snap, acct_ids, existing_txns)
    if gone:
        await _delete_absent(session, gone)
        stats.deleted_transactions = len(gone)
```

  Append to the end of the file:

```python
async def _void_relations(session: AsyncSession, ids: set[int]) -> None:
    """Clear the duplicate pointers and transfer links resting on `ids`.

    Provider-overlap reconciliation and link_transfers, which run right after
    ingest, re-match from whatever remains.
    """
    if not ids:
        return
    await session.execute(
        update(Transaction)
        .where(or_(Transaction.id.in_(ids), Transaction.duplicate_of_id.in_(ids)))
        .values(duplicate_of_id=None)
    )
    stale_links = await session.scalars(
        select(TransferLink).where(
            or_(TransferLink.outflow_id.in_(ids), TransferLink.inflow_id.in_(ids))
        )
    )
    for link in stale_links.all():
        await session.delete(link)


def _absent_from_replay(
    snap: Snapshot,
    acct_ids: dict[str, int],
    stored: dict[tuple[int, str], tuple[int, int, date, str]],
) -> set[int]:
    """Ids of stored rows a complete Plaid replay should have returned but did not.

    Only Plaid accounts the snapshot gives a replay floor are judged, and only their
    rows dated on or after that floor: older rows have aged out of the replay window,
    not been withdrawn.
    """
    plaid = {dto.id for dto in snap.accounts if dto.source == "plaid"}
    floors = {
        acct_ids[ext]: floor
        for ext, floor in snap.complete_replays.items()
        if ext in plaid and ext in acct_ids
    }
    if not floors:
        return set()
    returned = {
        (acct_ids[t.account_id], t.id) for t in snap.transactions if t.account_id in acct_ids
    }
    return {
        tid
        for key, (tid, _cents, posted, _desc) in stored.items()
        if key[0] in floors and posted >= floors[key[0]] and key not in returned
    }


async def _delete_absent(session: AsyncSession, gone: set[int]) -> None:
    """Delete withdrawn rows, leaving nothing pointing at them."""
    tags: dict[int, int | None] = {
        tid: sid
        for tid, sid in (
            await session.execute(
                select(Transaction.id, Transaction.series_id).where(Transaction.id.in_(gone))
            )
        ).all()
    }
    survivors = await session.scalars(
        select(Transaction).where(
            Transaction.duplicate_of_id.in_(gone), Transaction.id.not_in(gone)
        )
    )
    for survivor in survivors.all():
        # the other provider's copy becomes the counted row; carrying the bill's tag
        # keeps detection from mistaking it for a genuinely new, untagged charge
        if survivor.series_id is None and survivor.duplicate_of_id is not None:
            survivor.series_id = tags.get(survivor.duplicate_of_id)
    await _void_relations(session, gone)
    lost = {sid for sid in tags.values() if sid is not None}
    if lost:
        kept = set(
            await session.scalars(
                select(Transaction.series_id).where(
                    Transaction.series_id.in_(lost), Transaction.id.not_in(gone)
                )
            )
        )
        if orphaned := lost - kept:
            # with no tagged row left the series would keep firing `missed` events;
            # detection revives it if a genuinely new charge arrives
            await session.execute(
                update(RecurringSeries)
                .where(
                    RecurringSeries.id.in_(orphaned),
                    RecurringSeries.status == SeriesStatus.active,
                )
                .values(status=SeriesStatus.ended)
            )
    # transactions is AUTOINCREMENT (Task 4): these ids are never handed out again
    await session.execute(delete(Transaction).where(Transaction.id.in_(gone)))
```

  In `src/moneta/cli/main.py`, directly after the `updated_transactions` block
  (lines 157-160), add:

```python
    if report["ingest"].get("deleted_transactions"):
        console.print(
            f"{report['ingest']['deleted_transactions']} transaction(s) removed upstream."
        )
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_ingest.py tests/test_plaid.py tests/test_run.py tests/test_cli.py tests/test_transaction_overlap.py tests/test_overlap_accounting.py`
Expected: PASS. The last two files guard the correction cleanup that moved into
`_void_relations`.

- [ ] **Step 5: Update the docs.**

  `CLAUDE.md`, Plaid bullet. Replace
  ``so `sync --full` is a no-op for Plaid. It stores the bank's``
  with:

```markdown
so `sync --full` is a no-op for Plaid. A replay whose last page reports `transactions_update_status == HISTORICAL_UPDATE_COMPLETE` is complete: the adapter puts each account's earliest posted date in `Snapshot.complete_replays` (an account with no posted rows gets none; `concat_snapshots` keeps the earliest), and ingest deletes that Plaid account's stored rows dated on or after it that the replay no longer returned — never older rows, never another provider's, and nothing for an item that is not ready, failed or skipped. A deletion clears `duplicate_of_id` pointers to the row (the other provider's copy becomes the root and inherits its series tag), drops transfer links on it, and ends an active series left with no tagged row; `IngestStats.deleted_transactions` counts them. It stores the bank's
```

  `docs/PRD.md`, §6.1 Plaid row. Replace
  ``Replays its full `/transactions/sync` history (≤730 days) every run; dedup absorbs the overlap.``
  with:

```markdown
Replays its full `/transactions/sync` history (≤730 days) every run; dedup absorbs the overlap. Once an item's history has finished loading, a transaction the bank stops reporting is deleted (its Plaid copy only, and only if dated on or after the earliest transaction the replay still returns for that account).
```

  `docs/PRD.md`, §7 feature history. Replace
  `| Date | Release | What shipped |` + newline + `|---|---|---|` with:

```markdown
| Date | Release | What shipped |
|---|---|---|
| 2026-10-15 | **Plaid deletions follow the bank** | Once a Plaid item's history has finished loading (`HISTORICAL_UPDATE_COMPLETE`), a stored transaction its replay no longer returns is deleted: Plaid accounts only, only rows dated on or after the earliest transaction the replay returned for that account, and nothing while an item is still loading, paging or failing. A SimpleFIN duplicate of a deleted row becomes the counted copy and keeps its series tag, transfer links on the row are dropped, and an active series left with no transaction ends. `moneta sync` reports `N transaction(s) removed upstream.` |
```

  `docs/user-guide.md`, Plaid "Notes". Replace

```markdown
- Plaid replays its full history (up to 730 days) on every sync, so plain `moneta sync`
  suffices after linking — no `--full` needed for Plaid.
```

  with:

```markdown
- Plaid replays its full history (up to 730 days) on every sync, so plain `moneta sync`
  suffices after linking — no `--full` needed for Plaid.
- Once a bank's history has finished loading at Plaid, a transaction the bank stops
  reporting is removed on the next sync. Only its Plaid copy goes; a SimpleFIN copy of the
  same transaction stays and counts instead. Transactions older than the earliest one Plaid
  still returns for that account are kept, and nothing is removed while a bank is still
  loading or its sync fails.
```

  `docs/user-guide.md`, Syncing. Replace
  `new series, events); if items need your attention`
  with
  `new series, events, transactions corrected or removed upstream); if items need your attention`.

  `docs/user-guide.md`, Combined provider history. Replace
  `can dissolve a former duplicate and invalid transfer links are revalidated.`
  with:

```markdown
can dissolve a former duplicate and invalid transfer links are revalidated; a Plaid
transaction the bank stops reporting is deleted and its SimpleFIN duplicate becomes the
counted copy.
```

  `README.md`. Replace
  ``730 days) on every sync, so plain `moneta sync` suffices after `plaid-link`.``
  with:

```markdown
730 days) on every sync, so plain `moneta sync` suffices after `plaid-link`; once a
bank's history has finished loading, transactions it stops reporting are removed.
```

- [ ] **Step 6: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/ingest.py src/moneta/cli/main.py tests/test_ingest.py tests/test_plaid.py tests/test_run.py tests/test_cli.py CLAUDE.md docs/PRD.md docs/user-guide.md README.md
git commit -s -m "fix: delete Plaid transactions a complete replay no longer returns (#35)"
```

---

### Task 6: Retire price-change questions about vanished rows

**Files:**
- Modify: `src/moneta/pipelines/reconcile.py:99` and `166-168` (in
  `_retire_questions`)
- Test: `tests/test_transaction_overlap.py`
- Docs: `docs/user-guide.md`, `docs/PRD.md`

**Interfaces:**
- Consumes: nothing new. Task 5 is what makes a `price_change` payload's
  `transaction_id` able to name a deleted row.
- Produces: an open `price_change` item whose integer `payload["transaction_id"]`
  is not a stored transaction is superseded with the reason
  `"Transaction is no longer reported by its provider"`.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_transaction_overlap.py`:

```python
async def test_price_question_whose_transaction_is_gone_is_superseded(
    session: AsyncSession,
) -> None:
    a, _ = await accounts(session)
    series = await make_series(session, merchant="Acme Fitness", expected_cents=-12000)
    live = await make_txn(
        session,
        a,
        description="ACME FITNESS",
        merchant="Acme Fitness",
        amount_cents=-13000,
        series_id=series.id,
    )

    def question(transaction_id: int) -> ReviewItem:
        return ReviewItem(
            kind=ReviewKind.price_change,
            question="Did 'Acme Fitness' change price from $120.00 to $130.00?",
            payload={
                "series_id": series.id,
                "currency": "USD",
                "transaction_id": transaction_id,
                "merchant": "Acme Fitness",
                "old_cents": -12000,
                "new_cents": -13000,
                "occurred_on": "2026-07-01",
                "llm_flagged": True,
            },
        )

    stale, current = question(live.id + 1000), question(live.id)
    session.add_all([stale, current])
    stats = await reconcile(session)
    reason = "Transaction is no longer reported by its provider"
    assert stats.reviews_superseded == 1
    assert stale.status == ReviewStatus.resolved
    assert stale.resolution is not None
    assert stale.resolution["superseded_reason"] == reason
    assert current.status == ReviewStatus.open
```

- [ ] **Step 2: Run it and confirm it fails.**

Run: `uv run pytest -q tests/test_transaction_overlap.py`
Expected: the new test FAILS (`reviews_superseded == 0`).

- [ ] **Step 3: Implement.** In `_retire_questions`, directly after
  `txns = list(await session.scalars(select(Transaction)))`, add:

```python
    physical_ids = {t.id for t in txns}
```

  Replace the `price_change` branch:

```python
        elif item.kind == ReviewKind.price_change:
            evidence = item.payload.get("transaction_id")
            if item.payload.get("series_id") in orphaned:
                _supersede(item, "Series is supported only by suppressed observations", today)
            elif type(evidence) is int and evidence not in physical_ids:
                # the provider withdrew the charge; a "yes" would apply an amount no
                # stored transaction carries
                _supersede(item, "Transaction is no longer reported by its provider", today)
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `uv run pytest -q tests/test_transaction_overlap.py tests/test_events.py tests/test_autoreview.py`
Expected: PASS.

- [ ] **Step 5: Update the docs.**

  `docs/user-guide.md`, review queue. Replace
  `Skipped items come back next time. Invalid input skips cleanly`
  with:

```markdown
Skipped items come back next time, except a price-change question whose transaction the
provider no longer reports, which retires itself. Invalid input skips cleanly
```

  `docs/PRD.md`, in the 2026-10-15 **Plaid deletions follow the bank** row that
  Task 5 added. Replace
  `and an active series left with no transaction ends.`
  with
  `and an active series left with no transaction ends; an open price-change question about a deleted transaction retires itself.`

- [ ] **Step 6: Run the full gate, then commit.**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/reconcile.py tests/test_transaction_overlap.py docs/user-guide.md docs/PRD.md
git commit -s -m "fix: retire price-change questions whose transaction was withdrawn"
```
