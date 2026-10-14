# Automatic Merchant Merging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One real bill forms one recurring series, even when banks describe it
differently, while distinct bills from one merchant stay separate. No manual
step is involved.

**Architecture:**
- Stronger descriptor rules plus a data-learned merge pass canonicalize merchant
  names on every sync.
- A pure `pipelines/bills.py` splits one merchant's charges into bills, using
  amount bands and concurrent billing tracks.
- `detect_recurring` matches bills to series by existing tags and folds
  duplicates.
- Review decisions gain an optional amount, so each decision addresses one bill.

**Tech Stack:** Python 3.13, SQLAlchemy async + SQLite, Alembic, pytest
(asyncio auto mode), uv.

**Spec:** `docs/superpowers/specs/2026-10-13-merchant-merging-design.md`. Read it
first; this plan argues from it.

## Global Constraints

- Money is integer cents everywhere. Payload `amount_cents` is an **unsigned**
  int greater than 0. Negative = outflow in `Transaction.amount_cents` and
  `RecurringSeries.expected_cents`.
- Band step and decision tolerance: **20%** (`BILL_TOLERANCE = 0.20`).
- Merge floor: a target name has **at least 4 letters**. A token is a place when
  it follows **at least 3** distinct existing names.
- Tracks need **at least 3** dates (`_MIN_OCCURRENCES`). Monthly tolerance
  comes from `cadence.TOLERANCE`.
- A payload names an amount **only when its merchant has more than one bill**.
  Legacy payloads (no amount) are merchant-level and cover every bill of their
  merchant and direction.
- Precedence: an amount-specific decision beats a merchant-level one, then the
  highest `ReviewItem.id` wins.
- LLM output is classification only; it never supplies a money value.
- Pipelines commit; views don't. Enum columns load as plain `str`: compare with
  `==`.
- Commits use `git commit -s`, with no AI co-author trailers. The repo is
  **public**: no real merchant names, amounts or counts in code, tests, docs or
  commit messages. Use invented names (`Acme Fitness`, `Streamco`).
- Gate before every commit:
  `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests`.
  Output must be pristine. During iteration, run only the narrow test file.
- Work in `~/code/.worktrees/moneta/merchant-merging` (branch `merchant-merging`).

## Review Focus

1. **A variable bill**, such as a utility that changes amount every month, must
   stay one bill with today's LLM/review handling. Splitting it into amount
   bands would drop it from fixed costs. Pinned in Task 4
   (`test_variable_bill_is_not_split`) and Task 5
   (`test_variable_bill_still_goes_to_review`).
2. **A second sync after merging** must change nothing: no renames flip-flop
   and no series are re-created. Pinned in Task 3
   (`test_renormalize_is_idempotent`) and Task 5
   (`test_concurrent_subscriptions_are_stable_across_runs`).
3. **An old decision without an amount** must keep suppressing every bill of
   its merchant. Pinned in Task 5 (`test_merchant_level_decision_covers_every_bill`).
4. **Brand + product names** (`Streamco Energy`, `Streamco Plus`) must not
   merge into `Streamco`. Pinned in Task 3
   (`test_single_token_name_absorbs_only_a_place`).
5. **Folding a series** must leave no transaction or event pointing at a deleted
   series id. Pinned in Task 5 (`test_pre_merge_series_fold_into_one`).

---

### Task 1: Stronger descriptor rules

**Files:**
- Modify: `src/moneta/pipelines/normalize.py:1-22`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Produces: `rule_normalize(descriptor: str) -> str`. The signature is unchanged;
  the output drops wallet prefixes, ACH tails, addresses, legal tokens, `*`, and
  trailing `usa`/`us` + state.

- [ ] **Step 1: Write the failing tests.** Append the test below to
  `tests/test_normalize.py`, and change the two `"Bilt Payment Biltrent Web Id"`
  expectations to `"Bilt Payment Biltrent"`. Those are in
  `test_rule_normalize_strips_reference_tokens` and
  `test_renormalize_merges_fragmented_merchants`. `WEB ID: …` is an ACH tail,
  not part of the merchant.

```python
def test_rule_normalize_strips_wallets_legal_ach_address_and_state() -> None:
    assert rule_normalize("APLPAY TST* ACME GRILL 0ELMTON TX") == "Acme Grill 0Elmton"
    assert rule_normalize("ACME FITNESS LLC RIVERTON TX") == "Acme Fitness Riverton"
    assert rule_normalize("ACME FITNESS LLC 500 EAST MAIN ROAD RIVERTON TX USA") == "Acme Fitness"
    assert rule_normalize("STREAMCO *SUBSCRIPTION 3RD STREET SAN CA USA") == (
        "Streamco Subscription"
    )
    assert rule_normalize("DD *STREAMCOPASS SAN FRANCISCO CA") == "Streamcopass San Francisco"
    assert rule_normalize("GOOGLE PAY ACME CO") == "Acme"
    assert rule_normalize("ACME PAYROLL PPD ID: 1234567890") == "Acme Payroll"
    assert rule_normalize("TX") == "Tx"  # a lone token is never stripped to nothing
```

- [ ] **Step 2: Run it and confirm it fails.**
  Run `uv run pytest tests/test_normalize.py -q -p no:randomly`.
  Expected: FAIL. `APLPAY` is kept, and `Llc`/`Tx` remain.

- [ ] **Step 3: Implement.** Replace the regex block and `rule_normalize` in
  `src/moneta/pipelines/normalize.py`:

```python
_PREFIXES = re.compile(
    r"^(aplpay|apple pay|gpay|google pay|sq \*|tst\*|paypal \*|py \*|dd \*|sp \*"
    r"|amzn mktp us\*)\s*",
    re.IGNORECASE,
)
# ACH metadata ("WEB ID: 123…", "PPD ID: …") is not part of the merchant
_ACH_TAIL = re.compile(r"\b(?:web|ppd|ccd|tel)\s+id\b:?.*$", re.IGNORECASE)
# a street address and everything after it; runs before _STORE_NUM eats the number
_ADDRESS = re.compile(
    r"\b\d+(?:st|nd|rd|th)?\s+(?:[a-z]+\s+){0,3}?"
    r"(?:street|st|road|rd|avenue|ave|blvd|drive|dr|lane|ln|way|pkwy)\b.*$",
    re.IGNORECASE,
)
_LEGAL = re.compile(r"\b(?:llc|inc|corp|ltd)\b\.?,?", re.IGNORECASE)
_STORE_NUM = re.compile(r"(#\d+|(?<![\w-])\d{3,}(?![\w-]))")
# payment-reference tokens: 8+ alphanumeric chars containing at least one digit
_REF_TOKEN = re.compile(r"\b(?=[0-9a-zA-Z]*\d)[0-9a-zA-Z]{8,}\b")
_SPACES = re.compile(r"\s+")
_US_STATES = frozenset(
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv "
    "nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc pr".split()
)


def rule_normalize(descriptor: str) -> str:
    """Strip what banks wrap around a merchant name (design 2026-10-13 §1)."""
    s = descriptor.strip()
    while (stripped := _PREFIXES.sub("", s)) != s:  # "AplPay TST* Name" nests prefixes
        s = stripped
    s = _ACH_TAIL.sub("", s)
    s = _ADDRESS.sub("", s)
    s = _REF_TOKEN.sub("", s)
    s = _STORE_NUM.sub("", s)
    s = _LEGAL.sub("", s).replace("*", " ")
    tokens = _SPACES.sub(" ", s).strip(" -:").split(" ")
    # location tails, but never the last remaining token
    while len(tokens) > 1 and tokens[-1].lower() in ("usa", "us"):
        tokens.pop()
    if len(tokens) > 1 and tokens[-1].lower() in _US_STATES:
        tokens.pop()
    if len(tokens) > 1 and tokens[-1].lower() == "co":
        tokens.pop()
    s = " ".join(tokens).strip(" -:")
    return s.title() if s else descriptor.strip().title()
```

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_normalize.py -q -p no:randomly`. Expected: PASS.
  Then run `uv run pytest -q`. If another test pinned an old merchant string,
  update it only when the new name follows a rule in Step 3; otherwise fix the
  rule.

- [ ] **Step 5: Commit.**

```bash
git add src/moneta/pipelines/normalize.py tests/test_normalize.py
git commit -s -m "feat: strip wallet prefixes, legal suffixes, ACH tails and locations from descriptors"
```

---

### Task 2: Several series per merchant, and bill references

**Files:**
- Create: `src/moneta/migrations/versions/0007_series_per_bill.py`
- Modify: `src/moneta/db.py:16` (set `_HEAD = "0007"`)
- Modify: `src/moneta/models.py`, covering `series_key`/`recurring_cluster_item`
  (lines 44-65) and `RecurringSeries.__table_args__` (line 165)
- Test: `tests/test_migrations.py`, `tests/test_models.py`

**Interfaces:**
- Produces, all in `moneta.models`:
  - `BILL_TOLERANCE: float = 0.20`;
  - `BillRef = tuple[str, str, int | None]`, meaning (merchant, direction value,
    unsigned amount or None);
  - `amounts_match(a: int, b: int) -> bool`;
  - `bill_ref(payload: Mapping[str, object]) -> BillRef | None`;
  - `same_bill(a: BillRef, b: BillRef) -> bool`;
  - `series_ref(series: RecurringSeries) -> BillRef`;
  - `recurring_cluster_item(merchant: str, direction: str, amount_cents: int | None = None) -> ReviewItem`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_migrations.py`:

```python
async def test_0007_allows_several_series_per_merchant() -> None:
    engine, sm = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    async with engine.connect() as conn:
        uniques = await conn.run_sync(
            lambda c: inspect(c).get_unique_constraints("recurring_series")
        )
    assert uniques == []
    async with sm() as session:
        await make_series(session, merchant="Acme Fitness", expected_cents=-999)
        await make_series(session, merchant="Acme Fitness", expected_cents=-4999)
        await session.commit()
    await engine.dispose()
```

  Add `from tests.factories import make_series` to that file's imports. Then
  append to `tests/test_models.py`, adding the imports it needs:

```python
from moneta.models import bill_ref, recurring_cluster_item, same_bill


def test_bill_ref_reads_optional_amount() -> None:
    assert bill_ref({"merchant": "Acme", "direction": "outflow"}) == ("Acme", "outflow", None)
    assert bill_ref({"merchant": "Acme", "direction": "outflow", "amount_cents": 999}) == (
        "Acme",
        "outflow",
        999,
    )
    assert bill_ref({"merchant": "Acme", "direction": "outflow", "amount_cents": -5}) == (
        "Acme",
        "outflow",
        None,
    )
    assert bill_ref({"merchant": "Acme", "direction": "sideways"}) is None


def test_same_bill_tolerance_and_merchant_level() -> None:
    assert same_bill(("Acme", "outflow", None), ("Acme", "outflow", 999))
    assert same_bill(("Acme", "outflow", 1000), ("Acme", "outflow", 1200))
    assert not same_bill(("Acme", "outflow", 1000), ("Acme", "outflow", 1300))
    assert not same_bill(("Acme", "outflow", None), ("Acme", "inflow", None))
    assert not same_bill(("Acme", "outflow", None), ("Acmes", "outflow", None))


def test_recurring_cluster_item_names_amount_only_when_given() -> None:
    plain = recurring_cluster_item("Acme", "outflow")
    assert plain.question == "Is 'Acme' a recurring bill?"
    assert plain.payload == {"merchant": "Acme", "direction": "outflow"}
    priced = recurring_cluster_item("Acme", "outflow", 4999)
    assert priced.question == "Is 'Acme' ($49.99) a recurring bill?"
    assert priced.payload == {"merchant": "Acme", "direction": "outflow", "amount_cents": 4999}
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest tests/test_migrations.py tests/test_models.py -q -p no:randomly`.
  Expected: FAIL, with an ImportError for `bill_ref` and a unique-constraint
  `IntegrityError`.

- [ ] **Step 3: Implement.**

  Create `src/moneta/migrations/versions/0007_series_per_bill.py`:

```python
"""Allow several recurring series per merchant (design 2026-10-13 §3, §5)."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# 0001 created UNIQUE(merchant, direction) unnamed, so SQLite cannot drop it by
# name. Batch-recreate the table from an explicit definition without it.
_WITHOUT_UNIQUE = sa.Table(
    "recurring_series",
    sa.MetaData(),
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("merchant", sa.String(), nullable=False),
    sa.Column("direction", sa.String(), nullable=False),
    sa.Column("cadence", sa.String(), nullable=False),
    sa.Column("expected_cents", sa.Integer(), nullable=False),
    sa.Column("next_expected_on", sa.Date(), nullable=False),
    sa.Column("status", sa.String(), nullable=False),
    sa.Column("discretionary", sa.Boolean(), nullable=False, server_default=sa.false()),
)


def upgrade() -> None:
    with op.batch_alter_table("recurring_series", copy_from=_WITHOUT_UNIQUE, recreate="always"):
        pass


def downgrade() -> None:
    # fails loudly (IntegrityError) if a merchant now has several series
    with op.batch_alter_table("recurring_series") as batch:
        batch.create_unique_constraint(
            "uq_recurring_series_merchant_direction", ["merchant", "direction"]
        )
```

  In `src/moneta/db.py`, set `_HEAD = "0007"`.

  In `src/moneta/models.py`:
  - Change the `typing` import to `from typing import Any` plus
    `from collections.abc import Mapping`.
  - Delete `__table_args__` from `RecurringSeries`.
  - Replace `recurring_cluster_item` with the version below, and add the new
    helpers directly after `series_key`.

```python
BILL_TOLERANCE = 0.20
# (merchant, direction value, unsigned amount) — amount None = merchant-level
BillRef = tuple[str, str, int | None]


def amounts_match(a: int, b: int) -> bool:
    """Two unsigned amounts name the same bill: within 20% of the larger."""
    return abs(a - b) <= max(a, b) * BILL_TOLERANCE


def bill_ref(payload: Mapping[str, object]) -> BillRef | None:
    """The bill a recurring_cluster payload addresses (design 2026-10-13 §4)."""
    key = series_key(payload.get("merchant"), payload.get("direction"))
    if key is None:
        return None
    amount = payload.get("amount_cents")
    return key[0], key[1], amount if type(amount) is int and amount > 0 else None


def same_bill(a: BillRef, b: BillRef) -> bool:
    """A merchant-level ref (amount None) covers every bill of its merchant+direction."""
    if a[:2] != b[:2]:
        return False
    if a[2] is None or b[2] is None:
        return True
    return amounts_match(a[2], b[2])


def recurring_cluster_item(
    merchant: str, direction: str, amount_cents: int | None = None
) -> "ReviewItem":
    """The one recurring_cluster ReviewItem construction — question text and payload
    shape are pinned by tests. Used by detection, verification, and the API ledger.
    An amount is named only when the merchant has several bills (design 2026-10-13 §4)."""
    payload: dict[str, Any] = {"merchant": merchant, "direction": direction}
    question = f"Is {merchant!r} a recurring bill?"
    if amount_cents is not None:
        payload["amount_cents"] = amount_cents
        question = f"Is {merchant!r} (${dollars(amount_cents)}) a recurring bill?"
    return ReviewItem(kind=ReviewKind.recurring_cluster, question=question, payload=payload)
```

  After the `RecurringSeries` class, add:

```python
def series_ref(series: RecurringSeries) -> BillRef:
    return series.merchant, str(series.direction), abs(series.expected_cents)
```

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_migrations.py tests/test_models.py -q -p no:randomly`.
  Expected: PASS, including the existing `test_migrations_match_create_all_schema`
  and `test_head_constant_matches_script_directory`.

- [ ] **Step 5: Run the full gate, then commit.**

```bash
git add src/moneta/migrations/versions/0007_series_per_bill.py src/moneta/db.py src/moneta/models.py tests/test_migrations.py tests/test_models.py
git commit -s -m "feat: allow several recurring series per merchant; bill references for decisions"
```

---

### Task 3: Merge pass every sync

**Files:**
- Modify: `src/moneta/pipelines/normalize.py`, covering `renormalize_merchants`
  (lines 78-122) and a new `merge_targets`
- Modify: `src/moneta/pipelines/run.py`, covering imports, `SyncReport`, and the
  stage list after `normalize_merchants`
- Test: `tests/test_normalize.py`, `tests/test_run.py`

**Interfaces:**
- Consumes: `rule_normalize` and `looks_clean` from Task 1. Task 2 removed series
  uniqueness, so renames are never skipped for collisions.
- Produces:
  - `merge_targets(names: Iterable[str]) -> dict[str, str]`, mapping a name to
    the name it merges into (unmerged names are absent);
  - `renormalize_merchants(session) -> int`, the number of aliases changed. It
    now also merges, and renames `RecurringSeries.merchant` and
    `recurring_cluster` payload merchants;
  - `SyncReport.merchants_renamed: int = 0`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_normalize.py`:

```python
from moneta.models import AliasSource, RecurringSeries, ReviewKind
from moneta.pipelines.normalize import merge_targets, renormalize_merchants
from tests.factories import make_series

_PLACES = ["Kroger", "Kroger Elmton", "Target", "Target Elmton", "Lidl", "Lidl Elmton"]


def test_multi_token_name_absorbs_longer_names() -> None:
    assert merge_targets(["Acme Fitness", "Acme Fitness Riverton", "Acme Fitness Of Texlakeside"]) == {
        "Acme Fitness Riverton": "Acme Fitness",
        "Acme Fitness Of Texlakeside": "Acme Fitness",
    }


def test_single_token_name_absorbs_only_a_place() -> None:
    names = [*_PLACES, "Streamco", "Streamco Elmton", "Streamco Plus", "Streamco Energy Ven"]
    targets = merge_targets(names)
    assert targets["Streamco Elmton"] == "Streamco"
    assert "Streamco Plus" not in targets  # brand + product stays its own merchant
    assert "Streamco Energy Ven" not in targets
    assert merge_targets([*_PLACES, "Shyco", "Shyco 0Elmton"])["Shyco 0Elmton"] == "Shyco"


def test_merge_needs_four_letters_and_resolves_chains() -> None:
    assert "Abc Fitness" not in merge_targets(["Abc", "Abc Fitness"])
    chained = merge_targets([*_PLACES, "Shyco", "Shyco Elmton", "Shyco Elmton Hifi"])
    assert chained["Shyco Elmton Hifi"] == "Shyco"


async def test_renormalize_merges_names_and_renames_series_and_decisions(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for raw in ("ACME FITNESS LLC RIVERTON TX", "APLPAY ACME FITNESS"):
        await make_txn(session, acct, description=raw)
    await make_txn(session, acct, description="ACME FITNESS ELMTON")
    session.add(
        MerchantAlias(
            raw_descriptor="ACME FITNESS ELMTON", merchant="My Gym", source=AliasSource.manual
        )
    )
    await session.flush()
    await normalize_merchants(session, llm=None)  # "Acme Fitness Riverton", "Acme Fitness"
    series = await make_series(session, merchant="Acme Fitness Riverton")
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Is 'Acme Fitness Riverton' a recurring bill?",
            payload={"merchant": "Acme Fitness Riverton", "direction": "outflow"},
        )
    )
    await session.commit()

    changed = await renormalize_merchants(session)

    assert changed == 1
    txns = (await session.execute(select(Transaction))).scalars().all()
    by_raw = {t.description: t.merchant for t in txns}
    assert by_raw["ACME FITNESS LLC RIVERTON TX"] == "Acme Fitness"
    assert by_raw["ACME FITNESS ELMTON"] == "My Gym"  # manual aliases are never rewritten
    await session.refresh(series)
    assert series.merchant == "Acme Fitness"
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload["merchant"] == "Acme Fitness"


async def test_renormalize_is_idempotent(session: AsyncSession) -> None:
    acct = await make_account(session)
    for raw in ("ACME FITNESS LLC RIVERTON TX", "ACME FITNESS"):
        await make_txn(session, acct, description=raw)
    await normalize_merchants(session, llm=None)
    assert await renormalize_merchants(session) == 1
    assert await renormalize_merchants(session) == 0
```

  Append to `tests/test_run.py`:

```python
async def test_sync_merges_one_bills_descriptors(session: AsyncSession) -> None:
    snap = Snapshot(
        accounts=[
            AccountDTO(
                id="A-1",
                name="Checking",
                org_name="Bank",
                currency="USD",
                balance=Decimal("1000.00"),
                balance_date=date(2026, 7, 1),
            )
        ],
        transactions=[
            TransactionDTO(
                id=f"T-{i}",
                account_id="A-1",
                posted_on=date(2026, month, 3),
                amount=Decimal("-49.99"),
                description=desc,
                raw={},
            )
            for i, (month, desc) in enumerate(
                ((3, "ACME FITNESS LLC RIVERTON TX"), (4, "ACME FITNESS"), (5, "ACME FITNESS"),
                 (6, "ACME FITNESS LLC RIVERTON TX"))
            )
        ],
        holdings=[],
    )
    report = await run_sync(session, [FakeAdapter(snap)], llm=None, today=date(2026, 7, 9))
    assert report.merchants_renamed == 1
    series = (await session.execute(select(RecurringSeries))).scalars().all()
    assert [(s.merchant, s.expected_cents) for s in series] == [("Acme Fitness", -4999)]
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest tests/test_normalize.py tests/test_run.py -q -p no:randomly`.
  Expected: FAIL, because `merge_targets` doesn't exist and `merchants_renamed`
  is missing.

- [ ] **Step 3: Implement.** In `src/moneta/pipelines/normalize.py`:
  - Add `from collections.abc import Iterable` and `from sqlalchemy import update`
    to the imports.
  - Extend the `moneta.models` import with `RecurringSeries`.
  - Replace `renormalize_merchants` entirely with the code below. Its function-local
    `RecurringSeries` import goes away, because `models` does not import
    `pipelines`, so there is no cycle.

```python
_MIN_TARGET_LETTERS = 4
_MIN_PLACE_NAMES = 3


def _tokens(name: str) -> tuple[str, ...]:
    """Comparison tokens: lower-cased, leading digits dropped ("0Elmton" -> "elmton")."""
    return tuple(t for raw in name.lower().split() if (t := raw.lstrip("0123456789")))


def merge_targets(names: Iterable[str]) -> dict[str, str]:
    """name -> the shorter name it merges into (design 2026-10-13 §1).

    A multi-token name absorbs any name it is a token prefix of; a single-token name
    absorbs only itself plus one *place* — a token that follows 3+ distinct existing
    names, learned from the data. Brand + product names therefore stay apart.
    """
    keyed = {n: _tokens(n) for n in sorted(set(names))}
    by_tokens: dict[tuple[str, ...], str] = {}
    for name, toks in keyed.items():
        by_tokens.setdefault(toks, name)
    followers: dict[str, set[tuple[str, ...]]] = {}
    for toks in keyed.values():
        if len(toks) >= 2 and toks[:-1] in by_tokens:
            followers.setdefault(toks[-1], set()).add(toks[:-1])
    places = {t for t, prefixes in followers.items() if len(prefixes) >= _MIN_PLACE_NAMES}
    targets: dict[str, str] = {}
    for name, toks in keyed.items():
        if not toks:
            continue
        for size in range(1, len(toks)):  # shortest qualifying prefix wins
            target = by_tokens.get(toks[:size])
            if target is None or sum(c.isalpha() for c in target) < _MIN_TARGET_LETTERS:
                continue
            if size >= 2 or (len(toks) == 2 and toks[1] in places):
                targets[name] = target
                break
        else:
            if by_tokens[toks] != name:  # same tokens, different spelling
                targets[name] = by_tokens[toks]
    for name in list(targets):  # resolve chains: A -> B -> C becomes A -> C
        seen = {name}
        while (nxt := targets.get(targets[name])) is not None and nxt not in seen:
            seen.add(nxt)
            targets[name] = nxt
    return targets


async def renormalize_merchants(session: AsyncSession) -> int:
    """Re-apply rules to non-manual aliases, then merge names (design 2026-10-13 §1).

    Runs every sync and is idempotent. Renames follow onto series and recurring
    decisions; an old name whose aliases now split across several names is ambiguous,
    so its series is left for detection to untag and end. Manual aliases are never
    rewritten, but their names are merge targets.
    """
    aliases = list((await session.execute(select(MerchantAlias))).scalars())
    base: dict[int, str] = {}
    for alias in aliases:
        if alias.source == AliasSource.manual:
            base[alias.id] = alias.merchant
            continue
        candidate = rule_normalize(alias.raw_descriptor)
        base[alias.id] = candidate if looks_clean(candidate) else alias.merchant
    targets = merge_targets(base.values())
    rename: dict[str, str | None] = {}  # old merchant -> new (None = ambiguous)
    by_target: dict[str, list[str]] = {}
    changed = 0
    for alias in aliases:
        if alias.source == AliasSource.manual:
            continue
        final = targets.get(base[alias.id], base[alias.id])
        if final == alias.merchant:
            continue
        old = alias.merchant
        rename[old] = None if rename.get(old, final) != final else final
        if alias.source == AliasSource.llm and base[alias.id] != old:
            alias.source = AliasSource.rule  # the clean rule output replaced the LLM's name
        alias.merchant = final
        by_target.setdefault(final, []).append(alias.raw_descriptor)
        changed += 1
    for final, raws in by_target.items():
        await session.execute(
            update(Transaction).where(Transaction.description.in_(raws)).values(merchant=final)
        )
    renames = {old: new for old, new in rename.items() if new is not None}
    if renames:
        for series in (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.merchant.in_(renames))
            )
        ).scalars():
            series.merchant = renames[series.merchant]
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalars():
            merchant = item.payload.get("merchant")
            if isinstance(merchant, str) and merchant in renames:
                item.payload = {**item.payload, "merchant": renames[merchant]}
    await session.commit()
    return changed
```

  In `src/moneta/pipelines/run.py`:
  - Import `renormalize_merchants`.
  - Add `merchants_renamed: int = 0` to `SyncReport` after `normalized`.
  - Insert this line after `normalized = await normalize_merchants(session, llm)`:

```python
        # every sync: improved rules and name merges reach existing aliases, series
        # and decisions, so one bill's descriptors form one merchant (design 2026-10-13 §1)
        merchants_renamed = await renormalize_merchants(session)
```

  Then pass `merchants_renamed=merchants_renamed` to `SyncReport(...)`. The
  existing `/normalize/rerun` endpoint already calls `renormalize_merchants`, so
  it gains the merge for free.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_normalize.py tests/test_run.py -q -p no:randomly`.
  Expected: PASS. The sync test passes already, because the rename runs before
  detection. Build its DTOs exactly as `test_second_identical_sync_is_a_full_noop`
  does (copy any required fields this sketch omits), and add the imports it needs
  (`Decimal`, `RecurringSeries`).

- [ ] **Step 5: Run the full gate, then commit.**

```bash
git add src/moneta/pipelines/normalize.py src/moneta/pipelines/run.py tests/test_normalize.py tests/test_run.py
git commit -s -m "feat: merge one bill's merchant names on every sync"
```

---

### Task 4: Split a merchant's charges into bills (pure)

**Files:**
- Create: `src/moneta/pipelines/bills.py`
- Test: `tests/test_bills.py`

**Interfaces:**
- Consumes: `match_cadence`, `advance_expected_on` and `TOLERANCE` from
  `moneta.cadence`; `Cadence`, `Transaction` and `BILL_TOLERANCE` from
  `moneta.models`.
- Produces:
  - `amount_bands(txns: Sequence[Transaction]) -> list[list[Transaction]]`;
  - `concurrent_tracks(band: Sequence[Transaction]) -> list[list[Transaction]] | None`;
  - `split_bills(group: Sequence[Transaction]) -> list[list[Transaction]] | None`.
    `None` means the group stays one bill, handled as today.

  Every returned list is sorted by `(posted_on, id)`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_bills.py`:

```python
from datetime import date, timedelta

from moneta.models import Transaction
from moneta.pipelines.bills import amount_bands, concurrent_tracks, split_bills

_ids = iter(range(1, 10_000))


def _t(posted_on: date, cents: int) -> Transaction:
    return Transaction(id=next(_ids), posted_on=posted_on, amount_cents=cents, series_id=None)


def _monthly(day: int, months: range, cents: int, year: int = 2026) -> list[Transaction]:
    return [_t(date(year, m, day), cents) for m in months]


def test_amount_bands_chain_within_twenty_percent() -> None:
    txns = [_t(date(2026, 1, d), c) for d, c in ((1, -1000), (2, -1150), (3, -1300), (4, -2600))]
    bands = amount_bands(txns)
    assert [[abs(t.amount_cents) for t in b] for b in bands] == [[1000, 1150, 1300], [2600]]


def test_concurrent_same_price_subscriptions_become_tracks() -> None:
    band = sorted(
        _monthly(11, range(1, 7), -2000) + _monthly(15, range(4, 7), -2000),
        key=lambda t: t.posted_on,
    )
    tracks = concurrent_tracks(band)
    assert tracks is not None
    assert sorted(len(tr) for tr in tracks) == [3, 6]


def test_moved_billing_day_is_one_stream() -> None:
    band = _monthly(11, range(1, 4), -1500) + _monthly(18, range(4, 7), -1500)
    assert concurrent_tracks(band) is None


def test_weekly_band_is_never_split_into_tracks() -> None:
    band = [_t(date(2026, 1, 5) + timedelta(days=7 * i), -500) for i in range(12)]
    assert concurrent_tracks(band) is None


def test_subscription_with_orders_splits_off_the_subscription() -> None:
    sub = _monthly(15, range(3, 7), -999)
    orders = [
        _t(date(2026, 3, 3), -2350),
        _t(date(2026, 4, 9), -1720),
        _t(date(2026, 5, 20), -3100),
        _t(date(2026, 6, 2), -2640),
    ]
    bills = split_bills(sorted(sub + orders, key=lambda t: t.posted_on))
    assert bills is not None
    assert [[t.id for t in b] for b in bills] == [[t.id for t in sub]]


def test_variable_bill_is_not_split() -> None:
    group = [
        _t(date(2026, 3, 10), -8000),
        _t(date(2026, 4, 10), -17000),
        _t(date(2026, 5, 10), -9500),
        _t(date(2026, 6, 10), -12000),
    ]
    assert split_bills(group) is None


def test_clean_subscription_is_not_split() -> None:
    assert split_bills(_monthly(15, range(3, 7), -999)) is None


def test_payer_streams_at_different_amounts_split() -> None:
    start = date(2026, 5, 1)
    a = [_t(start + timedelta(days=14 * i), 150000) for i in range(5)]
    b = [_t(start + timedelta(days=7 + 14 * i), 200000) for i in range(5)]
    bills = split_bills(sorted(a + b, key=lambda t: t.posted_on))
    assert bills is not None
    assert sorted(len(b) for b in bills) == [5, 5]


def test_tiny_adjustment_leaves_only_the_bill() -> None:
    group = [
        _t(date(2026, 4, 27), -26665),
        _t(date(2026, 5, 12), -189),
        _t(date(2026, 5, 24), -22522),
        _t(date(2026, 6, 27), -25059),
    ]
    bills = split_bills(group)
    assert bills is not None
    assert [abs(t.amount_cents) for t in bills[0]] == [26665, 22522, 25059]
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest tests/test_bills.py -q -p no:randomly`.
  Expected: FAIL with a ModuleNotFoundError for `moneta.pipelines.bills`.

- [ ] **Step 3: Implement.** Create `src/moneta/pipelines/bills.py`:

```python
"""Split one merchant's charges into bills (design 2026-10-13 §2). Pure — no I/O.

A merchant can carry several bills (a subscription and its top-ups, two same-price
subscriptions, two income streams from one payer). Splitting happens only when a
schedule has extra charges alongside it; otherwise the group stays one bill and
detection handles it exactly as before, so a variable bill is never dropped.
"""

import itertools
from collections.abc import Sequence
from datetime import date

from moneta.cadence import TOLERANCE, advance_expected_on, match_cadence
from moneta.models import BILL_TOLERANCE, Cadence, Transaction

_MIN_TRACK_DATES = 3


def _by_date(txns: Sequence[Transaction]) -> list[Transaction]:
    return sorted(txns, key=lambda t: (t.posted_on, t.id))


def _dates(txns: Sequence[Transaction]) -> list[date]:
    return sorted({t.posted_on for t in txns})


def amount_bands(txns: Sequence[Transaction]) -> list[list[Transaction]]:
    """Chain charges by size: each joins the band of the next-smaller charge when at
    most 20% above it."""
    bands: list[list[Transaction]] = []
    for t in sorted(txns, key=lambda t: (abs(t.amount_cents), t.posted_on, t.id)):
        if bands and abs(t.amount_cents) <= abs(bands[-1][-1].amount_cents) * (
            1 + BILL_TOLERANCE
        ):
            bands[-1].append(t)
        else:
            bands.append([t])
    return [_by_date(b) for b in bands]


def _tracks(band: Sequence[Transaction], cadence: Cadence) -> list[list[Transaction]]:
    """Greedy: each charge extends the track whose next expected date is nearest."""
    tracks: list[list[Transaction]] = []
    for t in _by_date(band):
        same_day = next((tr for tr in tracks if tr[-1].posted_on == t.posted_on), None)
        if same_day is not None:  # a double-post is the same occurrence
            same_day.append(t)
            continue
        best: list[Transaction] | None = None
        best_gap = TOLERANCE[cadence] + 1
        for track in tracks:
            gap = abs((t.posted_on - advance_expected_on(track[-1].posted_on, cadence)).days)
            if gap < best_gap:
                best, best_gap = track, gap
        if best is None:
            tracks.append([t])
        else:
            best.append(t)
    return tracks


def concurrent_tracks(band: Sequence[Transaction]) -> list[list[Transaction]] | None:
    """The band's simultaneous subscriptions, or None when it is one stream.

    Splits only when every charge sits on a track of 3+ dates and two tracks overlap
    in time — a moved billing day is one track ending where another starts.
    """
    match = match_cadence(_dates(band))
    if match is not None and match[0] in (Cadence.weekly, Cadence.biweekly):
        return None
    for cadence in (Cadence.monthly, Cadence.annual):
        tracks = _tracks(band, cadence)
        if len(tracks) < 2 or any(len(_dates(tr)) < _MIN_TRACK_DATES for tr in tracks):
            continue
        if any(
            a[0].posted_on <= b[-1].posted_on and b[0].posted_on <= a[-1].posted_on
            for a, b in itertools.combinations(tracks, 2)
        ):
            return [_by_date(tr) for tr in tracks]
    return None


def split_bills(group: Sequence[Transaction]) -> list[list[Transaction]] | None:
    """Candidate bills for one (merchant, direction) group, or None to keep it whole."""
    bands = amount_bands(group)
    units: list[list[Transaction]] = []
    concurrent = False
    for band in bands:
        tracks = concurrent_tracks(band)
        if tracks is None:
            units.append(band)
        else:
            concurrent = True
            units.extend(tracks)
    scheduled: list[list[Transaction]] = []
    extras = False
    for unit in units:
        dates = _dates(unit)
        match = match_cadence(dates)
        if match is None:
            continue
        scheduled.append(unit)
        members = {t.id for t in unit}
        start, end = match[1], dates[-1]
        extras = extras or any(
            start <= t.posted_on <= end and t.id not in members for t in group
        )
    if scheduled and (concurrent or extras):
        return scheduled
    return None
```

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_bills.py -q -p no:randomly`. Expected: PASS.

- [ ] **Step 5: Run the full gate, then commit.**

```bash
git add src/moneta/pipelines/bills.py tests/test_bills.py
git commit -s -m "feat: split a merchant's charges into bills by amount band and billing track"
```

---

### Task 5: Detection per bill — match, fold, per-bill decisions

**Files:**
- Modify: `src/moneta/pipelines/recurring.py`: imports, `RecurringStats`, the
  `ForcedAnswer` docstring, and `detect_recurring` (lines 112-380)
- Test: `tests/test_recurring.py`

**Interfaces:**
- Consumes:
  - Task 4: `split_bills`.
  - Task 2: `BillRef`, `bill_ref`, `same_bill`, `amounts_match`,
    `recurring_cluster_item(…, amount_cents)`.
  - `LogicalData` from `moneta.logical`.
- Produces:
  - `RecurringStats.merged: int = 0`;
  - `detect_recurring(session, llm, today) -> RecurringStats`, with the same
    signature.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_recurring.py`:

```python
from moneta.models import recurring_cluster_item  # merge into the existing models import


async def _seed_sub_and_orders(session: AsyncSession) -> tuple[Account, list[Transaction]]:
    acct = await _seed_monthly(session, (3, 4, 5, 6), merchant="Streamco", cents=-999)
    orders = [
        await make_txn(session, acct, amount_cents=c, merchant="Streamco", posted_on=d)
        for d, c in (
            (date(2026, 3, 3), -2350),
            (date(2026, 4, 9), -1720),
            (date(2026, 5, 20), -3100),
            (date(2026, 6, 2), -2640),
        )
    ]
    return acct, orders


async def test_subscription_with_orders_forms_one_series(session: AsyncSession) -> None:
    _, orders = await _seed_sub_and_orders(session)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1 and stats.review == 0
    s = (await _series(session))[0]
    assert s.expected_cents == -999 and s.cadence == Cadence.monthly
    assert all(o.series_id is None for o in orders)


async def test_top_ups_are_untagged_from_an_existing_series(session: AsyncSession) -> None:
    _, orders = await _seed_sub_and_orders(session)
    old = await make_series(session, merchant="Streamco", expected_cents=-999)
    for t in (await session.execute(select(Transaction))).scalars():
        t.series_id = old.id  # pre-split state: one series swallowed everything
    await session.commit()
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0
    s = (await _series(session))[0]
    assert s.id == old.id
    assert all(o.series_id is None for o in orders)


async def test_variable_bill_still_goes_to_review(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((3, -8000), (4, -17000), (5, -9500), (6, -12000)):
        await make_txn(
            session, acct, amount_cents=cents, merchant="Util Co", posted_on=date(2026, month, 10)
        )
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert "amount_cents" not in item.payload  # one bill: merchant-level question


async def test_concurrent_subscriptions_are_stable_across_runs(session: AsyncSession) -> None:
    acct = await _seed_monthly(session, (1, 2, 3, 4, 5, 6), merchant="Chatco", day=11, cents=-2000)
    await _seed_monthly(session, (4, 5, 6), acct=acct, merchant="Chatco", day=15, cents=-2000)
    first = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert first.new_series == 2
    assert {s.next_expected_on for s in await _series(session)} == {
        date(2026, 7, 11),
        date(2026, 7, 15),
    }
    second = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert second.new_series == 0 and second.merged == 0
    assert len(await _series(session)) == 2


async def test_payer_streams_at_different_amounts_form_two_series(session: AsyncSession) -> None:
    acct = await make_account(session)
    start = date(2026, 5, 1)
    for i in range(5):
        await make_txn(session, acct, amount_cents=150000, merchant="Acme Corp",
                       posted_on=start + timedelta(days=14 * i))
        await make_txn(session, acct, amount_cents=200000, merchant="Acme Corp",
                       posted_on=start + timedelta(days=7 + 14 * i))
    await detect_recurring(session, llm=None, today=date(2026, 7, 9))
    series = await _series(session)
    assert sorted(s.expected_cents for s in series) == [150000, 200000]
    assert {s.cadence for s in series} == {Cadence.biweekly}


async def test_pre_merge_series_fold_into_one(session: AsyncSession) -> None:
    acct = await make_account(session)
    older = await make_series(session, merchant="Acme Fitness", expected_cents=-4999,
                              status=SeriesStatus.ended)
    newer = await make_series(session, merchant="Acme Fitness", expected_cents=-4999)
    for month in (1, 2, 3, 4, 5, 6):
        await make_txn(session, acct, amount_cents=-4999, merchant="Acme Fitness",
                       posted_on=date(2026, month, 3),
                       series_id=older.id if month <= 3 else newer.id)
    session.add(SeriesEvent(series_id=older.id, kind=EventKind.missed,
                            occurred_on=date(2026, 4, 3), details={}))
    session.add(ReviewItem(kind=ReviewKind.price_change, question="?",
                           payload={"series_id": older.id}))
    await session.commit()

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    assert stats.merged == 1
    (survivor,) = await _series(session)
    assert survivor.id == newer.id  # it tags the newest charge
    assert survivor.status == SeriesStatus.active
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert {t.series_id for t in txns} == {newer.id}
    events = (await session.execute(select(SeriesEvent))).scalars().all()
    assert {e.series_id for e in events} <= {newer.id}
    price = (await session.execute(
        select(ReviewItem).where(ReviewItem.kind == ReviewKind.price_change)
    )).scalar_one()
    assert price.payload["series_id"] == newer.id


async def test_per_bill_decision_only_forces_its_bill(session: AsyncSession) -> None:
    await _seed_sub_and_orders(session)
    orders_item = recurring_cluster_item("Streamco", "outflow", 2600)
    orders_item.status = ReviewStatus.resolved
    orders_item.resolution = {"is_recurring": False, "resolved_by": "manual"}
    session.add(orders_item)
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert [s.expected_cents for s in await _series(session)] == [-999]


async def test_merchant_level_decision_covers_every_bill(session: AsyncSession) -> None:
    await _seed_sub_and_orders(session)
    session.add(ReviewItem(kind=ReviewKind.recurring_cluster, question="?",
                           payload={"merchant": "Streamco", "direction": "outflow"},
                           status=ReviewStatus.resolved,
                           resolution={"is_recurring": False, "resolved_by": "manual"}))
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert await _series(session) == []


async def test_specific_decision_beats_newer_merchant_level(session: AsyncSession) -> None:
    await _seed_sub_and_orders(session)
    specific = recurring_cluster_item("Streamco", "outflow", 999)
    specific.status = ReviewStatus.resolved
    specific.resolution = {"is_recurring": True, "discretionary": True, "resolved_by": "manual"}
    session.add(specific)
    await session.flush()
    session.add(ReviewItem(kind=ReviewKind.recurring_cluster, question="?",
                           payload={"merchant": "Streamco", "direction": "outflow"},
                           status=ReviewStatus.resolved,
                           resolution={"is_recurring": False, "resolved_by": "manual"}))
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    (s,) = await _series(session)
    assert s.expected_cents == -999 and s.discretionary is True


async def test_split_bill_question_names_its_amount(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, cents in ((3, -1000), (4, -1150), (5, -1320), (6, -1500)):
        await make_txn(session, acct, amount_cents=cents, merchant="Gymco",
                       posted_on=date(2026, month, 15))
    for d in (date(2026, 3, 2), date(2026, 4, 25), date(2026, 6, 1)):
        await make_txn(session, acct, amount_cents=-6000, merchant="Gymco", posted_on=d)
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload["amount_cents"] == 1235
    assert item.question == "Is 'Gymco' ($12.35) a recurring bill?"


async def test_series_whose_txns_all_moved_ends(session: AsyncSession) -> None:
    await _seed_monthly(session, (4, 5, 6))
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    for t in (await session.execute(select(Transaction))).scalars():
        t.merchant = "Netflix Streaming"  # a rename this series wasn't carried through
    await session.commit()
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    old = next(s for s in await _series(session) if s.merchant == "Netflix")
    assert old.status == SeriesStatus.ended
```

  The factories already accept `series_id=` (`make_txn`) and `status=`
  (`make_series`) through `**kw`. Run `uv run ruff format tests/test_recurring.py`
  after pasting.

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest tests/test_recurring.py -q -p no:randomly`.
  Expected: the new tests FAIL. For example, `RecurringStats` has no `merged`,
  and the subscription-with-orders case gets no series.

- [ ] **Step 3: Implement.** In `src/moneta/pipelines/recurring.py`:

  Fix the imports:
  - add `from collections.abc import Sequence`;
  - add `from dataclasses import dataclass`;
  - add `from sqlalchemy import select, update`;
  - add `from moneta.logical import LogicalData, load_logical_data`;
  - add `from moneta.pipelines.bills import split_bills`;
  - in the `moneta.models` import, drop `series_key` and add `BillRef`,
    `amounts_match`, `bill_ref` and `same_bill`.

  Add `merged: int = 0` to `RecurringStats`. Change the `ForcedAnswer` docstring
  to `"""A resolved recurring_cluster ReviewItem's answer for one bill."""`.

  Replace everything from `async def detect_recurring(` to the end of the file
  with:

```python
@dataclass
class _Run:
    """State shared by every bill one detect_recurring call examines."""

    session: AsyncSession
    llm: Classifier | None
    today: date
    stats: RecurringStats
    logical: LogicalData
    foreign_series: set[int]
    open_refs: list[BillRef]
    resolved: list[tuple[BillRef, int, ForcedAnswer]]
    primary: str


def _forced(resolved: list[tuple[BillRef, int, ForcedAnswer]], ref: BillRef) -> ForcedAnswer | None:
    """The decision in force for a bill: amount-specific beats merchant-level, then the
    newest (design 2026-10-13 §4)."""
    covering = [
        (r[2] is not None, item_id, answer) for r, item_id, answer in resolved if same_bill(r, ref)
    ]
    return max(covering, key=lambda c: c[:2])[2] if covering else None


def _claim(
    unclaimed: list[RecurringSeries], bill: Sequence[Transaction], amount: int
) -> RecurringSeries | None:
    """The series tagging the bill's newest tagged charge, else the nearest amount within
    20% (design 2026-10-13 §3). Claimed series leave the pool."""
    by_id = {s.id: s for s in unclaimed}
    chosen = next(
        (by_id[t.series_id] for t in reversed(bill) if t.series_id in by_id), None
    )
    if chosen is None:
        near = [s for s in unclaimed if amounts_match(abs(s.expected_cents), amount)]
        chosen = min(
            near, key=lambda s: (abs(abs(s.expected_cents) - amount), s.id), default=None
        )
    if chosen is not None:
        unclaimed.remove(chosen)
    return chosen


async def _fold(session: AsyncSession, loser: RecurringSeries, survivor: RecurringSeries) -> None:
    """Collapse a pre-merge duplicate into the series its charges now feed."""
    await session.flush()  # survivor tags are written before the loser's are cleared
    await session.execute(
        update(SeriesEvent).where(SeriesEvent.series_id == loser.id).values(series_id=survivor.id)
    )
    await session.execute(
        update(Transaction).where(Transaction.series_id == loser.id).values(series_id=None)
    )
    for item in (
        await session.execute(select(ReviewItem).where(ReviewItem.kind == ReviewKind.price_change))
    ).scalars():
        if item.payload.get("series_id") == loser.id:
            item.payload = {**item.payload, "series_id": survivor.id}
    await session.delete(loser)


async def _detect_bill(
    ctx: _Run,
    merchant: str,
    direction: Direction,
    significant: list[Transaction],
    *,
    split: bool,
    unclaimed: list[RecurringSeries],
) -> tuple[RecurringSeries, set[int]] | None:
    """One candidate bill through cadence, stability, decisions and series matching.
    Returns the series it feeds and the series ids its charges were tagged to before."""
    if len(significant) < _MIN_OCCURRENCES:
        return None
    voted = {t.series_id for t in significant if t.series_id is not None}
    dates = sorted({t.posted_on for t in significant})  # dedup: double-posts aren't 0-day gaps
    match = match_cadence(dates)
    if match is None:
        cadence, recent = None, significant
    else:
        # stats come from the newest cadence-run so ancient price epochs don't skew them
        cadence, run_start = match
        recent = [t for t in significant if t.posted_on >= run_start]
    amounts = [abs(t.amount_cents) for t in recent]
    med = statistics.median(amounts)
    stable = all(abs(a - med) <= med * _AMOUNT_TOLERANCE for a in amounts)
    expected = -round(med) if direction == Direction.outflow else round(med)
    ref: BillRef = (merchant, str(direction), round(med))
    forced = _forced(ctx.resolved, ref)
    if forced is not None and forced.is_recurring is False:
        return None  # user-resolved as not recurring — suppress silently, forever
    discretionary = forced.discretionary if forced is not None else False
    forced_recurring = forced is not None and forced.is_recurring

    def _open_review(*, llm_flagged: bool = False) -> None:
        if any(same_bill(r, ref) for r in ctx.open_refs):
            return
        # a question names the amount only when the merchant has several bills
        amount = round(med) if split else None
        item = recurring_cluster_item(merchant, direction, amount)
        if llm_flagged:
            item.payload = {**item.payload, "llm_flagged": True}
        ctx.session.add(item)
        ctx.open_refs.append((merchant, str(direction), amount))
        ctx.stats.review += 1

    if cadence is None:
        if not forced_recurring:  # irregular timing needs a human
            # only ask when it plausibly IS a bill: steady amounts at bill-like intervals
            # (a single date is never bill-like timing — nothing to measure a gap from)
            if stable and len(dates) >= 2 and _median_gap(dates) >= _MIN_REVIEW_GAP_DAYS:
                _open_review()
            return None
        cadence = _closest_cadence(dates)
    elif not stable and not forced_recurring:
        answer = (
            await ctx.llm.classify_json(
                _LLM_PROMPT.format(
                    merchant=merchant,
                    lo=min(amounts),
                    med=round(med),
                    hi=max(amounts),
                    rows=[(t.amount_cents, t.posted_on.isoformat()) for t in recent],
                )
            )
            if ctx.llm
            else None
        )
        classification = answer.get("classification") if answer else None
        if classification == "habit":
            discretionary = True
        elif classification != "bill":
            # an actual (even malformed) LLM answer means the LLM already looked —
            # mirror verify_series: human-only from here, autoreview must not re-ask
            # the binary bill/one-off prompt and rubber-stamp it as a fixed cost.
            # A classify_json failure (answer is None) never looked; leave it askable.
            _open_review(llm_flagged=answer is not None)
            return None
    next_on = advance_expected_on(dates[-1], cadence)
    stale = _stale(dates[-1], cadence, ctx.today)
    series = _claim(unclaimed, significant, round(med))
    session, logical, stats = ctx.session, ctx.logical, ctx.stats
    if series is None:
        series = RecurringSeries(
            merchant=merchant,
            direction=direction,
            cadence=cadence,
            expected_cents=expected,
            next_expected_on=next_on,
            status=SeriesStatus.ended if stale else SeriesStatus.active,
            discretionary=discretionary,
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
        changing_currency = series.id in ctx.foreign_series
        if changing_currency:
            # Merchant/direction is the existing series identity. Rebuild its
            # monetary state from this currency and retire only derived tags;
            # source observations and classification history stay untouched.
            previous_currencies = {
                logical.accounts[logical.account_roots[txn.account_id]].currency
                for txn in logical.transactions
                if txn.series_id == series.id
            }
            for item in await session.scalars(
                select(ReviewItem).where(
                    ReviewItem.kind == ReviewKind.price_change,
                    ReviewItem.status == ReviewStatus.open,
                )
            ):
                if item.payload.get("series_id") == series.id and "currency" not in item.payload:
                    item.payload = {
                        **item.payload,
                        "currency": (
                            next(iter(previous_currencies))
                            if len(previous_currencies) == 1
                            else "ambiguous"
                        ),
                    }
            for txn in logical.transactions:
                if (
                    txn.series_id == series.id
                    and logical.accounts[logical.account_roots[txn.account_id]].currency
                    != ctx.primary
                ):
                    txn.series_id = None
            series.expected_cents = expected
            ctx.foreign_series.discard(series.id)
        advanced_on = max(series.next_expected_on, next_on)
        if stale:
            status = SeriesStatus.ended
        elif (
            series.status == SeriesStatus.ended
            and significant[-1].series_id is None
            and not changing_currency
        ):
            # the newest occurrence is untagged ⇒ genuinely new since the last run: an
            # ended series (auto- or manually) that charges again at cadence revives
            # (minor adjustments are never tagged, so they can't trigger this)
            status = SeriesStatus.active
        else:
            status = series.status
        if series.status == SeriesStatus.active and status == SeriesStatus.active:
            # a resumed series leaps next_expected_on over unexamined windows here,
            # and events.py only walks forward from the advanced value — emit misses
            # for any empty window the leap skips (revivals deliberately don't burst).
            # bound at the newest charge: windows past it were re-anchored by that
            # charge and belong to events.py's future-guarded loop, not this one
            grace_days = GRACE_DAYS[cadence]
            window = series.next_expected_on
            while window < dates[-1]:
                if not any(abs((d - window).days) <= grace_days for d in dates):
                    session.add(missed_event(series.id, window))
                window = advance_expected_on(window, cadence)
        changed = (
            changing_currency
            or series.next_expected_on != advanced_on
            or series.cadence != cadence
            or series.status != status
            or series.discretionary != discretionary
        )
        series.cadence = cadence
        series.next_expected_on = advanced_on
        series.status = status
        series.discretionary = discretionary
        if changed:
            stats.updated += 1
    for t in significant:
        t.series_id = series.id
    return series, voted


async def detect_recurring(
    session: AsyncSession, llm: Classifier | None, today: date
) -> RecurringStats:
    stats = RecurringStats()
    excluded, loan_payment_outflows = await _excluded_txn_ids(session)
    # bill-scoped: an answer about one bill (direction and, when named, amount) must
    # not force another (design 2026-10-13 §4)
    open_refs: list[BillRef] = []
    resolved: list[tuple[BillRef, int, ForcedAnswer]] = []
    for item in (
        await session.execute(
            select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
        )
    ).scalars():
        ref = bill_ref(item.payload)
        if ref is None:
            continue
        if item.status == ReviewStatus.open:
            open_refs.append(ref)
        elif isinstance(item.resolution, dict) and isinstance(
            item.resolution.get("is_recurring"), bool
        ):
            resolved.append(
                (
                    ref,
                    item.id,
                    ForcedAnswer(
                        is_recurring=item.resolution["is_recurring"],
                        discretionary=bool(item.resolution.get("discretionary")),
                    ),
                )
            )
    all_series = list(
        (await session.execute(select(RecurringSeries).order_by(RecurringSeries.id))).scalars()
    )
    pool: dict[tuple[str, str], list[RecurringSeries]] = {}
    for s in all_series:
        pool.setdefault((s.merchant, str(s.direction)), []).append(s)
    # series feed power's income/fixed-cost sums, so they must be single-currency:
    # only the primary currency's transactions can form or update a series
    primary = await primary_currency(session)
    logical = await load_logical_data(session)
    foreign_series = foreign_series_ids(logical, primary)
    txns = sorted(
        (
            t
            for t in logical.transactions
            if t.merchant is not None
            and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        ),
        key=lambda t: (t.posted_on, t.id),
    )
    # series left with no tagged txns this run (loan payments, a rename that moved every
    # charge away) are ended by the final sweep
    untagged_series: set[int] = set()
    # a description correction can re-derive a txn's merchant away from the series it
    # was tagged to — untag it so the old series doesn't keep stale occurrences and the
    # new group sees it; same-merchant corrections stay tagged (so they never look
    # "genuinely new" to the ended-series revival check)
    series_by_id = {s.id: s for s in all_series}
    for t in txns:
        if t.series_id is not None:
            owner = series_by_id.get(t.series_id)
            if owner is not None and owner.merchant != t.merchant:
                untagged_series.add(owner.id)
                t.series_id = None

    # loan-like payment outflows no longer belong to a merchant series — their
    # per-account cadence/amount is derived in queries.loan_payment_stats instead
    # (design 2026-07-16 §3).
    for t in txns:
        if t.id in loan_payment_outflows and t.series_id is not None:
            untagged_series.add(t.series_id)
            t.series_id = None

    groups: dict[tuple[str, Direction], list[Transaction]] = {}
    for t in txns:
        if t.id in excluded or t.merchant is None:
            continue
        direction = Direction.outflow if t.amount_cents < 0 else Direction.inflow
        groups.setdefault((t.merchant, direction), []).append(t)

    ctx = _Run(session, llm, today, stats, logical, foreign_series, open_refs, resolved, primary)
    folded: set[int] = set()
    for (merchant, direction), group in groups.items():
        bills = split_bills(group)
        split = bills is not None
        if bills is None:
            # one bill, handled as before: drop sub-scale adjustments
            scale = statistics.median([abs(t.amount_cents) for t in group])
            bills = [[t for t in group if abs(t.amount_cents) >= scale * _MINOR_FRACTION]]
        unclaimed = list(pool.get((merchant, str(direction)), []))
        claims: list[tuple[RecurringSeries, list[Transaction], set[int]]] = []
        for bill in sorted(bills, key=len, reverse=True):
            result = await _detect_bill(
                ctx, merchant, direction, bill, split=split, unclaimed=unclaimed
            )
            if result is not None:
                claims.append((result[0], bill, result[1]))
        for series, bill, voted in claims:
            members = {t.id for t in bill}
            for t in group:  # top-ups and orders leave the subscription they rode on
                if t.series_id == series.id and t.id not in members:
                    t.series_id = None
            for loser in [s for s in unclaimed if s.id in voted]:
                unclaimed.remove(loser)
                await _fold(session, loser, series)
                folded.add(loser.id)
                stats.merged += 1

    # Groups that no longer match a cadence (trailing noise, shrunk by exclusions) never
    # reach the stale check above — sweep every still-active series by its own txns.
    newest_by_series: dict[int, date] = {}
    for txn in logical.transactions:
        if txn.series_id is not None:
            newest_by_series[txn.series_id] = max(
                newest_by_series.get(txn.series_id, txn.posted_on), txn.posted_on
            )
    for series in all_series:
        if series.id in folded:
            continue
        if series.status != SeriesStatus.active or series.id in foreign_series:
            continue
        # detect_recurring tags every occurrence it matches, so an active series
        # normally always has tagged txns. Zero tagged txns means either there's no
        # evidence to judge (leave it alone) or every txn it had was just untagged
        # (loan payments, merchant moved) — end it here.
        last_seen = newest_by_series.get(series.id)
        if last_seen is None:
            if series.id in untagged_series:
                series.status = SeriesStatus.ended
                stats.updated += 1
            continue
        if _stale(last_seen, series.cadence, today):
            series.status = SeriesStatus.ended
            stats.updated += 1
    await session.commit()
    return stats
```

  **Faithfulness check before running.** Diff your new `_detect_bill`
  existing-series branch against the deleted code (`git diff`). The only
  intended changes are:
  - `ctx.` prefixes (`ctx.primary`, `ctx.foreign_series`);
  - `continue` → `return None`;
  - `run` → `recent`;
  - `existing.get(...)` → `_claim(...)`.

  Restore any other drift.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_recurring.py tests/test_run.py -q -p no:randomly`.
  Expected: PASS, including every pre-existing recurring test. Then run
  `uv run pytest -q` to catch callers in other files (events, digest, api,
  overlap).

- [ ] **Step 5: Run the full gate, then commit.**

```bash
git add src/moneta/pipelines/recurring.py tests/test_recurring.py
git commit -s -m "feat: detect recurring series per bill; fold merged duplicates"
```

---

### Task 6: Decisions per bill across review, overlap, reconcile and the API

**Files:**
- Modify: `src/moneta/queries.py` (add `bill_amount`)
- Modify: `src/moneta/pipelines/transaction_overlap.py:243-247, 291-319`
- Modify: `src/moneta/pipelines/reconcile.py:22-25, 123, 139-146`
- Modify: `src/moneta/pipelines/review.py`, covering imports, `review_context`
  (around line 208), `apply_resolution` (lines 264-267 and 395-415),
  `autoreview_items` (lines 480-488) and `verify_series` (lines 575-640)
- Modify: `src/moneta/api.py:436-461` (`_series_ledger_item`)
- Test: create `tests/test_bill_decisions.py`

**Interfaces:**
- Consumes: Task 2 helpers.
- Produces:
  - `queries.bill_amount(session, series: RecurringSeries) -> int | None`;
  - `transaction_overlap.human_recurring_refs(items: list[ReviewItem]) -> list[BillRef]`,
    which replaces `human_recurring_keys`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_bill_decisions.py`:

```python
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.models import (
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    recurring_cluster_item,
)
from moneta.pipelines.review import apply_resolution, verify_series
from moneta.pipelines.transaction_overlap import reviewed_merchant_occurrences
from tests.factories import make_account, make_series, make_txn


def _human(item: ReviewItem, is_recurring: bool) -> ReviewItem:
    item.status = ReviewStatus.resolved
    item.resolution = {"is_recurring": is_recurring, "resolved_by": "manual"}
    return item


async def test_specific_not_a_bill_ends_only_its_series(session: AsyncSession) -> None:
    sub = await make_series(session, merchant="Streamco", expected_cents=-999)
    orders = await make_series(session, merchant="Streamco", expected_cents=-2600)
    item = recurring_cluster_item("Streamco", "outflow", 2600)
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_recurring": False}, resolved_by="manual")
    assert orders.status == SeriesStatus.ended
    assert sub.status == SeriesStatus.active


async def test_human_answer_on_one_bill_does_not_block_llm_on_another(
    session: AsyncSession,
) -> None:
    session.add(_human(recurring_cluster_item("Streamco", "outflow", 2600), False))
    other = recurring_cluster_item("Streamco", "outflow", 999)
    merchant_level = recurring_cluster_item("Streamco", "outflow")
    session.add_all([other, merchant_level])
    await session.flush()
    await apply_resolution(session, other, {"is_recurring": True}, resolved_by="llm")
    assert other.status == ReviewStatus.resolved
    with pytest.raises(ValueError):
        await apply_resolution(session, merchant_level, {"is_recurring": True}, resolved_by="llm")


async def test_reviewed_occurrences_respect_bill_amounts(session: AsyncSession) -> None:
    acct = await make_account(session)
    sub = await make_txn(session, acct, amount_cents=-999, merchant="Streamco")
    order = await make_txn(session, acct, amount_cents=-2600, merchant="Streamco")
    session.add(_human(recurring_cluster_item("Streamco", "outflow", 999), True))
    await session.flush()
    roots = {sub.id: sub.id, order.id: order.id}
    assert await reviewed_merchant_occurrences(session, roots) == {sub.id}


class _BillLLM:
    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return {"classification": "bill", "confident": True}


async def test_verify_names_amounts_when_a_merchant_has_several_series(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for cents in (-999, -2600):
        s = await make_series(session, merchant="Streamco", expected_cents=cents)
        await make_txn(session, acct, amount_cents=cents, merchant="Streamco", series_id=s.id)
    await session.commit()
    stats = await verify_series(session, _BillLLM())
    assert stats.verified == 2
    items = (await session.execute(select(ReviewItem))).scalars().all()
    assert sorted(i.payload["amount_cents"] for i in items) == [999, 2600]


async def test_not_a_bill_endpoint_targets_one_of_several_series(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        sub = await make_series(session, merchant="Streamco", expected_cents=-999)
        orders = await make_series(session, merchant="Streamco", expected_cents=-2600)
        sub_id, orders_id = sub.id, orders.id
        await session.commit()
    r = await client.post(f"/recurring/{orders_id}/not-a-bill")
    assert r.status_code == 200
    async with sessionmaker() as session:
        statuses = {
            s.id: s.status for s in (await session.execute(select(RecurringSeries))).scalars()
        }
        item = (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalar_one()
    assert statuses == {sub_id: SeriesStatus.active, orders_id: SeriesStatus.ended}
    assert item.payload["amount_cents"] == 2600
```

  The `client` fixture is defined in `tests/test_api.py` (and separately in
  `test_api_overlap.py` and `test_e2e.py`). Move the `test_api.py` one into
  `tests/conftest.py` unchanged, delete it from `test_api.py`, and leave the
  other two files' local fixtures alone (they shadow it).

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest tests/test_bill_decisions.py -q -p no:randomly`.
  Expected: FAIL. Both series end, the LLM path raises, and the payload has no
  amount.

- [ ] **Step 3: Implement.**

  `src/moneta/queries.py`: extend the `moneta.models` import with
  `RecurringSeries`, then add:

```python
async def bill_amount(session: AsyncSession, series: RecurringSeries) -> int | None:
    """The amount a decision about this series names — only when its merchant has
    several series (design 2026-10-13 §4); otherwise decisions stay merchant-level."""
    count = await session.scalar(
        select(func.count())
        .select_from(RecurringSeries)
        .where(
            RecurringSeries.merchant == series.merchant,
            RecurringSeries.direction == series.direction,
        )
    )
    return abs(series.expected_cents) if (count or 0) > 1 else None
```

  `src/moneta/pipelines/transaction_overlap.py`: import `BillRef`, `bill_ref` and
  `same_bill` from `moneta.models`. In `_decisions`, replace the
  `recurring_cluster` condition with:

```python
            ref = bill_ref(item.payload) if item.kind == ReviewKind.recurring_cluster else None
            if (
                ref is not None
                and txn.merchant is not None
                and same_bill(ref, (txn.merchant, direction, abs(txn.amount_cents)))
                and isinstance(item.resolution.get("is_recurring"), bool)
            ):
```

  Replace `human_recurring_keys` and the `recurring` test in
  `reviewed_merchant_occurrences`:

```python
def human_recurring_refs(items: list[ReviewItem]) -> list[BillRef]:
    return [
        ref
        for item in items
        if item.kind == ReviewKind.recurring_cluster
        and item.status == ReviewStatus.resolved
        and item.resolution
        and isinstance(item.resolution.get("is_recurring"), bool)
        and item.resolution.get("resolved_by") not in ("llm", "reconciliation")
        and (ref := bill_ref(item.payload)) is not None
    ]
```

```python
    recurring = human_recurring_refs(list(await session.scalars(select(ReviewItem))))
    return {
        roots[t.id]
        for t in await session.scalars(select(Transaction))
        if t.id in roots
        and (
            t.description in manual
            or (
                t.merchant is not None
                and any(
                    same_bill(
                        r,
                        (
                            t.merchant,
                            "outflow" if t.amount_cents < 0 else "inflow",
                            abs(t.amount_cents),
                        ),
                    )
                    for r in recurring
                )
            )
        )
    }
```

  `src/moneta/pipelines/reconcile.py`:
  - Import `human_recurring_refs` instead of `human_recurring_keys`.
  - Import `bill_ref` and `same_bill` from `moneta.models`.
  - Set `recurring_choices = human_recurring_refs(...)`.
  - Replace the `recurring_cluster` branch with:

```python
        elif item.kind == ReviewKind.recurring_cluster:
            ref = bill_ref(item.payload)
            key = (item.payload.get("merchant"), item.payload.get("direction"))
            if ref is not None and any(same_bill(ref, c) for c in recurring_choices):
                _supersede(item, "Recurring cluster already has a human decision", today)
            elif key in raw_keys and key not in live_keys:
                _supersede(
                    item, "Recurring cluster is supported only by suppressed observations", today
                )
```

  `src/moneta/pipelines/review.py`:
  - Import changes: from `moneta.models`, drop `series_key` and add
    `amounts_match`, `bill_ref`, `same_bill` and `series_ref`. Import
    `human_recurring_refs` instead of `human_recurring_keys`, and import
    `bill_amount` from `moneta.queries`.
  - `review_context`, `recurring_cluster` branch: after
    `merchant = item.payload.get("merchant")`, add
    `ref = bill_ref(item.payload)`. Change `matching` to:

```python
        amount = ref[2] if ref is not None else None
        matching = [
            t
            for t in logical.transactions
            if t.merchant == merchant
            and (amount is None or amounts_match(abs(t.amount_cents), amount))
        ]
```

  - Add this helper above `apply_resolution`:

```python
async def _human_decision_covers(session: AsyncSession, item: ReviewItem) -> bool:
    ref = bill_ref(item.payload)
    protected = human_recurring_refs(list(await session.scalars(select(ReviewItem))))
    return ref is not None and any(same_bill(ref, p) for p in protected)
```

  - `apply_resolution` guard:

```python
    if item.kind == ReviewKind.recurring_cluster and resolved_by != "manual":
        if await _human_decision_covers(session, item):
            raise ValueError("Automatic recurring resolution cannot override a human decision")
```

  - `apply_resolution` series effects: replace both `recurring_cluster` branches
    with:

```python
    elif item.kind == ReviewKind.recurring_cluster and isinstance(
        resolution.get("is_recurring"), bool
    ):
        stmt = select(RecurringSeries).where(
            RecurringSeries.merchant == item.payload.get("merchant")
        )
        direction = item.payload.get("direction")
        if direction is not None:
            stmt = stmt.where(RecurringSeries.direction == direction)
        ref = bill_ref(item.payload)
        amount = ref[2] if ref is not None else None
        for series in (await session.execute(stmt)).scalars():
            if amount is not None and not amounts_match(abs(series.expected_cents), amount):
                continue  # a per-bill answer leaves the merchant's other bills alone
            if resolution["is_recurring"] is False:
                # detection's force map suppresses future runs; end the live series now
                # so fixed costs stop counting it immediately, not after the stale sweep
                if series.status == SeriesStatus.active:
                    series.status = SeriesStatus.ended
            else:
                series.discretionary = bool(resolution.get("discretionary"))
```

  - `autoreview_items` guard: replace the `protected_keys` lines with
    `if await _human_decision_covers(session, item):`, keeping the body.
  - `verify_series`: replace the `seen` set and its check, and pass an amount
    when building the item:

```python
    seen = [
        ref
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalars()
        if (ref := bill_ref(item.payload)) is not None
    ]
```

```python
        if any(same_bill(series_ref(series), r) for r in seen):
            continue
```

```python
        amount = await bill_amount(session, series)
        item = recurring_cluster_item(series.merchant, series.direction, amount)
        seen.append((series.merchant, str(series.direction), amount))
```

  `src/moneta/api.py`, `_series_ledger_item`: import `bill_amount` from
  `moneta.queries`, and `amounts_match` and `bill_ref` from `moneta.models`.
  Then replace the item lookup:

```python
        amount = await bill_amount(session, series)

        def addresses(candidate: ReviewItem) -> bool:
            ref = bill_ref(candidate.payload)
            if ref is None:
                return False
            if amount is None:
                return ref[2] is None
            return ref[2] is not None and amounts_match(ref[2], amount)

        item = next(
            (
                candidate
                for candidate in (
                    await session.execute(
                        select(ReviewItem)
                        .where(
                            ReviewItem.kind == ReviewKind.recurring_cluster,
                            ReviewItem.payload["merchant"].as_string() == series.merchant,
                            ReviewItem.payload["direction"].as_string() == series.direction,
                        )
                        .order_by(ReviewItem.id)
                    )
                ).scalars()
                if addresses(candidate)
            ),
            None,
        )
        if item is None:
            item = recurring_cluster_item(series.merchant, series.direction, amount)
            session.add(item)
            await session.flush()
        return series, item
```

  Finally, run `grep -rn "human_recurring_keys\|series_key(" src tests`. The
  only remaining hits should be `series_key` itself in `models.py` and its use
  inside `bill_ref`. Fix any other hit.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest tests/test_bill_decisions.py -q -p no:randomly`, then
  `uv run pytest -q`. Expected: PASS, including every overlap, reconcile,
  autoreview, api and cli test. Single-series merchants behave exactly as
  before.

- [ ] **Step 5: Run the full gate, then commit.**

```bash
git add src/moneta/queries.py src/moneta/pipelines/transaction_overlap.py src/moneta/pipelines/reconcile.py src/moneta/pipelines/review.py src/moneta/api.py tests/test_bill_decisions.py tests/conftest.py tests/test_api.py
git commit -s -m "feat: recurring decisions address one bill when a merchant has several"
```

---

### Task 7: Docs

**Files:**
- Modify: `CLAUDE.md`, covering the "Pipeline order", "Series lifecycle",
  "Discretionary semantics" and "LLM verification" bullets
- Modify: `docs/user-guide.md`, covering the recurring/review sections and the
  `normalize --rerun` description if present
- Modify: `docs/PRD.md`, covering the feature table plus a history row
  "2026-10-13 | Automatic merchant merging; per-bill series"

- [ ] **Step 1: Update the docs.** Required content, in plain prose with
  invented examples only:
  - **Pipeline order** now includes `renormalize_merchants` (rules + merge,
    every sync) right after normalize.
  - A merchant can have **several series**. A group splits into bills only when
    a schedule has extra charges alongside it, or two same-price schedules
    overlap. Otherwise detection is unchanged.
  - **Series matching**: by the newest tagged charge, else the nearest amount
    within 20%. Merged duplicates are folded, and `RecurringStats.merged` counts
    them.
  - **Decisions**: `recurring_cluster` payloads may carry `amount_cents`, only
    when the merchant has several bills. Payloads without it are
    merchant-level. Specific beats merchant-level, then newest. The force-map
    description changes from `(merchant, direction)` to bill refs
    (`models.bill_ref`/`same_bill`).
  - **`moneta review`** questions can read `Is 'X' ($12.34) a recurring bill?`.
  - **Known limitations**: from the spec's last section.

- [ ] **Step 2: Scan for private data.**
  Run `git diff --cached -U0 | grep -nE '\$[0-9]+|[0-9]{3,}'`, plus the same
  check over the whole branch with `git diff main -U0 -- docs CLAUDE.md`.
  Every amount must be an invented example. Never name a real merchant or
  employer.

- [ ] **Step 3: Run the full gate, then commit.**

```bash
git add CLAUDE.md docs/user-guide.md docs/PRD.md
git commit -s -m "docs: automatic merchant merging and per-bill series"
```

---

### Task 8: Acceptance on a rehearsal copy, then live

Private. Nothing from this task goes into git, PRs or issues. Artifacts live in
`~/reports/moneta-merge-2026-10-13/` (mode 0700, files 0600).

- [ ] **Step 1: Snapshot the baseline.**

```bash
R=~/reports/moneta-merge-2026-10-13; mkdir -p -m 700 $R
cp ~/.config/moneta/moneta.db $R/rehearsal.db && chmod 600 $R/rehearsal.db
sqlite3 $R/rehearsal.db "select id, merchant, direction, cadence, expected_cents, status from recurring_series order by merchant" > $R/series-before.txt
```

  Also record `moneta power --json` against the main checkout, which still runs
  the old code, with `MONETA_CONFIG_DIR` pointed at a scratch config dir that
  uses `rehearsal.db`. Save it to `$R/power-before.json`, and record the
  open-review count.

- [ ] **Step 2: Rebuild on the rehearsal copy with the new code**, from the
  worktree. Running migrations through `init_db` applies 0007. Then call
  `renormalize_merchants` and `rebuild_logical_derived(session, llm, today)`,
  with the vLLM classifier from settings, in a short `uv run python -` script.
  Run it twice. The second run's `RecurringStats` must be all zeros, and
  `renormalize_merchants` must return 0.

- [ ] **Step 3: Check the acceptance criteria** (spec, "Testing"):
  - The previously split merchants each collapsed to one active series. These
    are the series-group list in the private handoff: the subscription, the
    takeout and the wireless bill, among others.
  - Merchants with several streams kept them apart: the two income streams, the
    subscription vs orders, and the multi-product brand.
  - Compare `power --json` before and after. Fixed costs and income drop only
    by removed duplicates; explain every line that moved.
  - The open-review count stays within a few of the baseline.

  Write `$R/findings.md` with the before/after per merchant.

- [ ] **Step 4: Report to the owner and wait.** Send the findings summary in
  chat, without pasting real figures into any public artifact. Then ask before
  touching the live DB.

- [ ] **Step 5: Go live**, on the owner's yes:
  - run `moneta backup`;
  - merge the branch per the owner's choice;
  - run `moneta sync` from the updated checkout;
  - repeat the Step 3 checks on the live DB, and confirm a second `moneta sync`
    reports `merchants_renamed: 0`, `merged: 0` and `new_series: 0`.
