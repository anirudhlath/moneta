# LLM Recurring Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an LLM is configured, every deterministically detected recurring series gets an LLM second opinion, and expected-amount changes are LLM-gated; anything not confidently waved through goes to the human review queue.

**Architecture:** A new `verify_series` step runs in `run_sync` between `detect_recurring` and `emit_series_events`, using the ReviewItem table as its verification ledger (no schema change). `emit_series_events` gains the `llm` parameter and gates its >5% price-drift updates. A new `price_change` ReviewKind carries deferred amount changes through the existing resolution path.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, Pydantic v2, pytest + pytest-asyncio, FastAPI, typer.

**Spec:** `docs/superpowers/specs/2026-07-09-llm-recurring-verification-design.md`

## Global Constraints

- Verification gate before EVERY commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — all four must pass, output pristine (a deprecation warning is a failure).
- Money is integer cents (`*_cents: int`); dollars only as formatted strings at boundaries (`f"{abs(cents) / 100:.2f}"`). The LLM never supplies a money value — it only gates whether a deterministic value is applied or queued.
- Sign convention: negative = outflow, positive = inflow.
- Enum columns load as plain `str`; compare with `==`, never `is`, never `.name`.
- Pipelines commit (`session.commit()` inside); views don't.
- Run everything with `uv run …`; never pip.
- Tests are async functions taking the `session: AsyncSession` fixture (conftest.py); factories in `tests/factories.py` (`make_account`, `make_txn`, `make_series`).

---

### Task 1: `verify_series` pipeline step

**Files:**
- Modify: `src/moneta/pipelines/review.py`
- Test: `tests/test_autoreview.py`

**Interfaces:**
- Consumes: existing `Classifier` protocol (`classify_json(prompt) -> dict | None`), `ReviewItem`, `RecurringSeries`, `Transaction` models; `ScriptedLLM` test fake already defined at the top of `tests/test_autoreview.py`.
- Produces: `class VerifyStats(BaseModel)` with `verified: int = 0` and `flagged: int = 0`; `async def verify_series(session: AsyncSession, llm: Classifier | None) -> VerifyStats` (commits). Task 4 wires both into `run_sync`/`SyncReport`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_autoreview.py` (add `RecurringSeries`, `ReviewKind`, `SeriesStatus` to the existing `moneta.models` import; add `verify_series`, `VerifyStats` to the `moneta.pipelines.review` import; add `make_series` to the factories import; `date` is already imported):

```python
async def _series_with_occurrences(session: AsyncSession) -> RecurringSeries:
    acct = await make_account(session)
    series = await make_series(session, merchant="Netflix")
    await make_txn(
        session,
        acct,
        amount_cents=-1599,
        merchant="Netflix",
        posted_on=date(2026, 6, 15),
        series_id=series.id,
    )
    return series


async def test_verify_confident_yes_writes_resolved_ledger_item(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": True}})
    stats = await verify_series(session, llm)
    assert stats == VerifyStats(verified=1, flagged=0)
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.resolved
    assert item.resolution == {"is_recurring": True, "resolved_by": "llm"}
    # settled: a second run asks nothing
    assert await verify_series(session, llm) == VerifyStats()
    assert len(llm.prompts) == 1


async def test_verify_prompt_carries_amounts_and_dates(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": True}})
    await verify_series(session, llm)
    assert "15.99" in llm.prompts[0] and "2026-06-15" in llm.prompts[0]


async def test_verify_unconfident_flags_for_human(session: AsyncSession) -> None:
    series = await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": False}})
    stats = await verify_series(session, llm)
    assert stats == VerifyStats(verified=0, flagged=1)
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.open
    assert item.payload["llm_flagged"] is True
    assert series.status == SeriesStatus.active  # keeps counting until a human rules


async def test_verify_confident_no_flags_rather_than_suppresses(session: AsyncSession) -> None:
    series = await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": False, "confident": True}})
    stats = await verify_series(session, llm)
    assert stats == VerifyStats(verified=0, flagged=1)
    assert series.status == SeriesStatus.active  # the LLM never suppresses determinism
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.open


async def test_verify_skips_merchants_with_existing_items(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Is 'Netflix' a recurring bill?",
            payload={"merchant": "Netflix", "direction": "outflow"},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": True}})
    assert await verify_series(session, llm) == VerifyStats()
    assert llm.prompts == []


async def test_verify_skips_ended_series(session: AsyncSession) -> None:
    await make_series(session, merchant="Old Gym", status=SeriesStatus.ended)
    llm = ScriptedLLM({"Old Gym": {"is_recurring": True, "confident": True}})
    assert await verify_series(session, llm) == VerifyStats()
    assert llm.prompts == []


async def test_verify_without_llm_is_noop(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    assert await verify_series(session, None) == VerifyStats()
    assert (await session.execute(select(ReviewItem))).scalar_one_or_none() is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_autoreview.py -q`
Expected: ImportError — `verify_series` / `VerifyStats` don't exist.

- [ ] **Step 3: Implement `verify_series`**

In `src/moneta/pipelines/review.py`: add `BaseModel` to imports (`from pydantic import BaseModel`), add `RecurringSeries`, `SeriesStatus` to the `moneta.models` import. Add below `_RECURRING_PROMPT`:

```python
_VERIFY_PROMPT = """You are double-checking automatic recurring-bill detection.
Is this one recurring bill/subscription/income stream (vs. habitual spending
like groceries, gas, or dining that merely happens on a regular rhythm)?
Merchant: {merchant!r}; direction: {direction}; cadence: {cadence}; \
expected amount: ${expected}; recent occurrences: {samples}
Respond with JSON: {{"is_recurring": true/false, "confident": true/false}}
Set confident=true ONLY if you are sure either way."""
```

Add after `autoreview_items` (module docstring already frames this file as the LLM-review boundary):

```python
class VerifyStats(BaseModel):
    verified: int = 0
    flagged: int = 0


async def verify_series(session: AsyncSession, llm: Classifier | None) -> VerifyStats:
    """LLM second opinion on deterministically detected series.

    A recurring_cluster ReviewItem (open or resolved) is the per-merchant
    verification ledger: confident "yes" is recorded resolved (which also feeds
    detect_recurring's force map); anything else opens a human item flagged
    llm_flagged so autoreview never re-asks the LLM. The LLM never suppresses a
    deterministic detection — flagged series stay active until a human rules.
    """
    stats = VerifyStats()
    if llm is None:
        return stats
    seen = {
        item.payload.get("merchant")
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalars()
    }
    series_list = (
        (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.active)
            )
        )
        .scalars()
        .all()
    )
    for series in series_list:
        if series.merchant in seen:
            continue
        txns = (
            (
                await session.execute(
                    select(Transaction)
                    .where(Transaction.series_id == series.id)
                    .order_by(Transaction.posted_on.desc())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        answer = await llm.classify_json(
            _VERIFY_PROMPT.format(
                merchant=series.merchant,
                direction=series.direction,
                cadence=series.cadence,
                expected=f"{abs(series.expected_cents) / 100:.2f}",
                samples=[
                    (t.posted_on.isoformat(), f"{abs(t.amount_cents) / 100:.2f}") for t in txns
                ],
            )
        )
        payload: dict[str, Any] = {"merchant": series.merchant, "direction": series.direction}
        if answer and answer.get("is_recurring") is True and answer.get("confident") is True:
            session.add(
                ReviewItem(
                    kind=ReviewKind.recurring_cluster,
                    question=f"Is {series.merchant!r} a recurring bill?",
                    payload=payload,
                    status=ReviewStatus.resolved,
                    resolution={"is_recurring": True, "resolved_by": "llm"},
                )
            )
            stats.verified += 1
        else:
            session.add(
                ReviewItem(
                    kind=ReviewKind.recurring_cluster,
                    question=f"Is {series.merchant!r} a recurring bill?",
                    payload={**payload, "llm_flagged": True},
                )
            )
            stats.flagged += 1
    await session.commit()
    return stats
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_autoreview.py -q`
Expected: all pass.

- [ ] **Step 5: Full gate, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/review.py tests/test_autoreview.py
git commit -m "feat: verify_series — LLM second opinion on deterministic recurring detections"
```

---

### Task 2: `autoreview_items` skips `llm_flagged` items

**Files:**
- Modify: `src/moneta/pipelines/review.py:181` (top of the `autoreview_items` loop)
- Test: `tests/test_autoreview.py`

**Interfaces:**
- Consumes: `ReviewItem.payload["llm_flagged"]` convention established in Task 1.
- Produces: guarantee that items opened by verification (and Task 5's price gate) are human-only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_autoreview.py`:

```python
async def test_autoreview_skips_llm_flagged_items(session: AsyncSession) -> None:
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Is 'Costco' a recurring bill?",
            payload={"merchant": "Costco", "direction": "outflow", "llm_flagged": True},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"Costco": {"is_recurring": True, "confident": True}})
    assert await autoreview_items(session, llm) == 0
    assert llm.prompts == []  # the LLM already looked once; re-asking is circular
    assert len(await _open_items(session)) == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_autoreview.py::test_autoreview_skips_llm_flagged_items -q`
Expected: FAIL — `autoreview_items` returns 1 and the item resolves.

- [ ] **Step 3: Implement the skip**

At the top of the `for item in items:` loop in `autoreview_items`:

```python
    for item in items:
        if item.payload.get("llm_flagged"):
            continue  # opened because the LLM already looked — human-only
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_autoreview.py -q`
Expected: all pass.

- [ ] **Step 5: Full gate, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/review.py tests/test_autoreview.py
git commit -m "feat: autoreview skips llm_flagged items — verification questions are human-only"
```

---

### Task 3: resolving "not recurring" ends the live series

**Files:**
- Modify: `src/moneta/pipelines/review.py` (`apply_resolution`)
- Test: `tests/test_autoreview.py`

**Interfaces:**
- Consumes: `apply_resolution(session, item, resolution, resolved_by)` (existing).
- Produces: `is_recurring: false` on a `recurring_cluster` item immediately ends matching active series (today it only feeds the force map, and the stale sweep takes ~3 cadence periods to catch up).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_autoreview.py` (add `Direction` to the models import, `apply_resolution` to the review import):

```python
async def test_not_recurring_resolution_ends_live_series(session: AsyncSession) -> None:
    series = await make_series(session, merchant="Costco")
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="Is 'Costco' a recurring bill?",
        payload={"merchant": "Costco", "direction": "outflow"},
    )
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_recurring": False})
    assert series.status == SeriesStatus.ended
    assert item.status == ReviewStatus.resolved


async def test_not_recurring_resolution_leaves_other_direction_alone(
    session: AsyncSession,
) -> None:
    series = await make_series(session, merchant="Costco", direction=Direction.inflow)
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="Is 'Costco' a recurring bill?",
        payload={"merchant": "Costco", "direction": "outflow"},
    )
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_recurring": False})
    assert series.status == SeriesStatus.active
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_autoreview.py -q -k not_recurring_resolution`
Expected: first test FAILS (series stays active); second passes vacuously.

- [ ] **Step 3: Implement in `apply_resolution`**

Add a branch before the final two lines (`item.status = …`), alongside the existing `merchant`/`transfer_pair` branches:

```python
    elif item.kind == ReviewKind.recurring_cluster and resolution.get("is_recurring") is False:
        # detection's force map suppresses future runs; end the live series now so
        # fixed costs stop counting it immediately instead of after the stale sweep
        stmt = select(RecurringSeries).where(
            RecurringSeries.merchant == item.payload.get("merchant"),
            RecurringSeries.status == SeriesStatus.active,
        )
        direction = item.payload.get("direction")
        if direction is not None:
            stmt = stmt.where(RecurringSeries.direction == direction)
        for series in (await session.execute(stmt)).scalars():
            series.status = SeriesStatus.ended
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_autoreview.py -q`
Expected: all pass.

- [ ] **Step 5: Full gate, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/review.py tests/test_autoreview.py
git commit -m "fix: resolving 'not recurring' ends the live series immediately"
```

---

### Task 4: wire `verify_series` into `run_sync`, `SyncReport`, and the CLI sync line

**Files:**
- Modify: `src/moneta/pipelines/run.py`
- Modify: `src/moneta/cli/main.py:27-37` (sync output)
- Test: `tests/test_run.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `verify_series`, `VerifyStats` from Task 1.
- Produces: `SyncReport.verify: VerifyStats`; pipeline order ingest → normalize → transfers → auto-review → recurring → **verify** → events. (`emit_series_events` is still called as `emit_series_events(session, today)`; Task 5 changes that signature.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_run.py` (it already imports `run_sync`, `RecordingAdapter`, `make_account`, `make_txn`, `date`, `Any`):

```python
async def test_sync_verifies_new_deterministic_series(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session,
            acct,
            amount_cents=-1599,
            merchant="Netflix",
            posted_on=date(2026, month, 9),
        )

    class VerifyLLM:
        async def classify_json(self, prompt: str) -> dict[str, Any] | None:
            if "Netflix" in prompt and "recurring bill" in prompt:
                return {"is_recurring": True, "confident": True}
            return None

    report = await run_sync(session, RecordingAdapter(), llm=VerifyLLM(), today=date(2026, 7, 9))
    assert report.recurring.new_series == 1
    assert report.verify == VerifyStats(verified=1, flagged=0)


async def test_sync_without_llm_reports_zero_verification(session: AsyncSession) -> None:
    report = await run_sync(session, RecordingAdapter(), llm=None, today=date(2026, 7, 9))
    assert report.verify == VerifyStats()
```

(Import `VerifyStats` from `moneta.pipelines.review` at the top.)

Append to `tests/test_cli.py` (imports there already include `runner`, `app`; add `SyncReport`, stats models as shown):

```python
def test_sync_prints_verification_line(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from moneta.pipelines.ingest import IngestStats
    from moneta.pipelines.recurring import RecurringStats
    from moneta.pipelines.review import VerifyStats
    from moneta.pipelines.run import SyncReport
    from moneta.pipelines.transfers import TransferStats

    report = SyncReport(
        ingest=IngestStats(),
        normalized=0,
        transfers=TransferStats(),
        auto_resolved=0,
        recurring=RecurringStats(),
        verify=VerifyStats(verified=2, flagged=1),
        events=0,
    ).model_dump(mode="json")

    def fake_request(method: str, path: str, *args: object, **kwargs: object) -> object:
        return report if path == "/sync" else []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "LLM verified 2 series; flagged 1 for review." in result.output
```

Before writing this test, check the exact signature of the existing `fake_request` in `test_sync_full_flag_requests_full_sync` (tests/test_cli.py:43) and mirror it exactly — including how `params` is passed — and mirror how that test constructs its report if it already builds one (reuse its shape rather than inventing a second).

If `IngestStats`/`TransferStats` have required fields (check `src/moneta/pipelines/ingest.py` and `transfers.py`), pass zeros explicitly.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_run.py tests/test_cli.py -q`
Expected: FAIL — `SyncReport` has no `verify` field / attribute errors.

- [ ] **Step 3: Implement**

`src/moneta/pipelines/run.py` — extend the review import and wire the step:

```python
from moneta.pipelines.review import VerifyStats, autoreview_items, verify_series
```

```python
class SyncReport(BaseModel):
    ingest: IngestStats
    normalized: int
    transfers: TransferStats
    auto_resolved: int
    recurring: RecurringStats
    verify: VerifyStats
    events: int
```

In `run_sync`, after `recurring = await detect_recurring(...)`:

```python
    recurring = await detect_recurring(session, llm, today)
    # second opinion on what detection produced, before events fire on it
    verify = await verify_series(session, llm)
    events = await emit_series_events(session, today)
    return SyncReport(
        ingest=ingest,
        normalized=normalized,
        transfers=transfers,
        auto_resolved=auto_resolved,
        recurring=recurring,
        verify=verify,
        events=events,
    )
```

`src/moneta/cli/main.py` — after the `auto_resolved` block:

```python
    verify = report["verify"]
    if verify["verified"] or verify["flagged"]:
        console.print(
            f"LLM verified {verify['verified']} series; "
            f"flagged {verify['flagged']} for review."
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_run.py tests/test_cli.py tests/test_e2e.py -q`
Expected: all pass (e2e exercises `/sync` end-to-end and must absorb the new report field transparently).

- [ ] **Step 5: Full gate, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/run.py src/moneta/cli/main.py tests/test_run.py tests/test_cli.py
git commit -m "feat: run verify_series in sync; report and print verification counts"
```

---

### Task 5: `price_change` ReviewKind + LLM gate in `emit_series_events`

**Files:**
- Modify: `src/moneta/models.py:78-81` (ReviewKind)
- Modify: `src/moneta/pipelines/events.py`
- Modify: `src/moneta/pipelines/run.py` (call site)
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: `Classifier` protocol; `ReviewItem`/`ReviewKind`/`ReviewStatus` models.
- Produces: `ReviewKind.price_change`; new signature `async def emit_series_events(session: AsyncSession, llm: Classifier | None, today: date) -> int`; item payload contract used by Task 6: `{"series_id": int, "merchant": str, "old_cents": int, "new_cents": int, "occurred_on": "YYYY-MM-DD", "llm_flagged": True}`.

- [ ] **Step 1: Update existing call sites for the new signature**

`src/moneta/models.py`:

```python
class ReviewKind(StrEnum):
    merchant = "merchant"
    transfer_pair = "transfer_pair"
    recurring_cluster = "recurring_cluster"
    price_change = "price_change"
```

In `tests/test_events.py`, change every `emit_series_events(session, today=…)` call to `emit_series_events(session, llm=None, today=…)` (seven call sites across the five existing tests). In `src/moneta/pipelines/run.py`, change the call to `emit_series_events(session, llm, today)`.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_events.py` (add `Any` import from `typing`, `ReviewItem`, `ReviewKind`, `ReviewStatus` to the models import):

```python
class PriceLLM:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        self.prompts.append(prompt)
        return self.answer


async def _drifted_series(session: AsyncSession) -> tuple[Any, Any]:
    acct = await make_account(session)
    s = await make_series(session, next_expected_on=date(2026, 8, 15))
    txn = await make_txn(
        session,
        acct,
        amount_cents=-1899,
        merchant="Netflix",
        posted_on=date(2026, 7, 15),
        series_id=s.id,
    )
    return s, txn


async def test_price_change_confident_yes_applies(session: AsyncSession) -> None:
    s, _ = await _drifted_series(session)
    llm = PriceLLM({"is_price_change": True, "confident": True})
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 1
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.price_increase
    assert s.expected_cents == -1899


async def test_price_change_unconfident_queues_item_once(session: AsyncSession) -> None:
    s, _ = await _drifted_series(session)
    llm = PriceLLM({"is_price_change": True, "confident": False})
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 0
    assert s.expected_cents == -1599  # not applied
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == ReviewKind.price_change and item.status == ReviewStatus.open
    assert item.payload == {
        "series_id": s.id,
        "merchant": "Netflix",
        "old_cents": -1599,
        "new_cents": -1899,
        "occurred_on": "2026-07-15",
        "llm_flagged": True,
    }
    # second sync: open item suppresses both the re-ask and a duplicate item
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 0
    assert len(llm.prompts) == 1
    assert (await session.execute(select(ReviewItem))).scalar_one() is item


async def test_price_change_denied_resolution_suppresses(session: AsyncSession) -> None:
    s, _ = await _drifted_series(session)
    session.add(
        ReviewItem(
            kind=ReviewKind.price_change,
            question="Did 'Netflix' change price from $15.99 to $18.99?",
            payload={
                "series_id": s.id,
                "merchant": "Netflix",
                "old_cents": -1599,
                "new_cents": -1899,
                "occurred_on": "2026-07-15",
                "llm_flagged": True,
            },
            status=ReviewStatus.resolved,
            resolution={"is_price_change": False, "resolved_by": "manual"},
        )
    )
    await session.flush()
    llm = PriceLLM({"is_price_change": True, "confident": True})
    assert await emit_series_events(session, llm=llm, today=date(2026, 7, 16)) == 0
    assert llm.prompts == []
    assert s.expected_cents == -1599
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_events.py -q`
Expected: new tests FAIL (TypeError on `llm=` kwarg until the signature changes, then behavioral failures); the five updated legacy tests must pass once the signature exists.

- [ ] **Step 4: Implement the gate**

`src/moneta/pipelines/events.py` — new imports and prompt:

```python
from moneta.llm import Classifier
from moneta.models import (
    Cadence,
    EventKind,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
    Transaction,
)
```

```python
_PRICE_PROMPT = """You are double-checking a detected price change on a recurring bill.
Series: {merchant!r}, expected ${old} {cadence}; latest charge ${new} on {posted_on}.
Is this a genuine new price for the same bill (vs. a one-off or unrelated charge)?
Respond with JSON: {{"is_price_change": true/false, "confident": true/false}}
Set confident=true ONLY if you are sure either way."""


async def _confirms_price_change(
    llm: Classifier, series: RecurringSeries, latest: Transaction
) -> bool:
    answer = await llm.classify_json(
        _PRICE_PROMPT.format(
            merchant=series.merchant,
            old=f"{abs(series.expected_cents) / 100:.2f}",
            cadence=series.cadence,
            new=f"{abs(latest.amount_cents) / 100:.2f}",
            posted_on=latest.posted_on.isoformat(),
        )
    )
    return (
        answer is not None
        and answer.get("is_price_change") is True
        and answer.get("confident") is True
    )
```

New signature plus suppression preload at the top of `emit_series_events`:

```python
async def emit_series_events(
    session: AsyncSession, llm: Classifier | None, today: date
) -> int:
    emitted = 0
    # price-change questions already in flight (open) or answered "no" (denied)
    open_series: set[int] = set()
    denied: set[tuple[int, int]] = set()
    for item in (
        await session.execute(select(ReviewItem).where(ReviewItem.kind == ReviewKind.price_change))
    ).scalars():
        sid = item.payload.get("series_id")
        if not isinstance(sid, int):
            continue
        if item.status == ReviewStatus.open:
            open_series.add(sid)
        elif isinstance(item.resolution, dict) and item.resolution.get("is_price_change") is False:
            new_cents = item.payload.get("new_cents")
            if isinstance(new_cents, int):
                denied.add((sid, new_cents))
```

Replace the drift block (currently `if drift > _PRICE_CHANGE_THRESHOLD:` through `emitted += 1`) with:

```python
            if (
                drift > _PRICE_CHANGE_THRESHOLD
                and s.id not in open_series
                and (s.id, latest.amount_cents) not in denied
            ):
                if llm is None or await _confirms_price_change(llm, s, latest):
                    session.add(
                        SeriesEvent(
                            series_id=s.id,
                            kind=EventKind.price_increase,
                            occurred_on=latest.posted_on,
                            details={"old_cents": s.expected_cents, "new_cents": latest.amount_cents},
                        )
                    )
                    s.expected_cents = latest.amount_cents
                    emitted += 1
                else:
                    session.add(
                        ReviewItem(
                            kind=ReviewKind.price_change,
                            question=(
                                f"Did {s.merchant!r} change price from "
                                f"${abs(s.expected_cents) / 100:.2f} to "
                                f"${abs(latest.amount_cents) / 100:.2f}?"
                            ),
                            payload={
                                "series_id": s.id,
                                "merchant": s.merchant,
                                "old_cents": s.expected_cents,
                                "new_cents": latest.amount_cents,
                                "occurred_on": latest.posted_on.isoformat(),
                                "llm_flagged": True,
                            },
                        )
                    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_events.py -q`
Expected: all pass.

- [ ] **Step 6: Full gate, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/models.py src/moneta/pipelines/events.py src/moneta/pipelines/run.py tests/test_events.py
git commit -m "feat: LLM-gate price changes; unconfident drifts queue a price_change review"
```

---

### Task 6: `price_change` resolution path (context, apply, API validation)

**Files:**
- Modify: `src/moneta/pipelines/review.py` (`review_context`, `apply_resolution`)
- Modify: `src/moneta/api.py:246-259` (resolve endpoint validation)
- Test: `tests/test_autoreview.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: payload contract from Task 5.
- Produces: resolving `{"is_price_change": true}` sets `series.expected_cents = payload["new_cents"]` and emits the `price_increase` SeriesEvent; `false` resolves only (Task 5's `denied` set reads it). API rejects non-bool `is_price_change` with 422.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_autoreview.py` (add `EventKind`, `SeriesEvent` to the models import):

```python
def _price_change_item(series_id: int) -> ReviewItem:
    return ReviewItem(
        kind=ReviewKind.price_change,
        question="Did 'Netflix' change price from $15.99 to $18.99?",
        payload={
            "series_id": series_id,
            "merchant": "Netflix",
            "old_cents": -1599,
            "new_cents": -1899,
            "occurred_on": "2026-07-15",
            "llm_flagged": True,
        },
    )


async def test_price_change_resolution_true_applies_amount(session: AsyncSession) -> None:
    series = await make_series(session)
    item = _price_change_item(series.id)
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_price_change": True})
    assert series.expected_cents == -1899
    ev = (await session.execute(select(SeriesEvent))).scalar_one()
    assert ev.kind == EventKind.price_increase
    assert ev.occurred_on == date(2026, 7, 15)
    assert ev.details == {"old_cents": -1599, "new_cents": -1899}
    assert item.status == ReviewStatus.resolved


async def test_price_change_resolution_false_resolves_without_applying(
    session: AsyncSession,
) -> None:
    series = await make_series(session)
    item = _price_change_item(series.id)
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_price_change": False})
    assert series.expected_cents == -1599
    assert (await session.execute(select(SeriesEvent))).scalar_one_or_none() is None
    assert item.status == ReviewStatus.resolved
```

Append to `tests/test_api.py`, mirroring `test_review_resolve_recurring_cluster_validates_and_applies` (tests/test_api.py:178) — seed via a direct DB session the way that test does:

```python
async def test_review_resolve_price_change_validates_and_applies(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        series = await make_series(session)
        session.add(
            ReviewItem(
                kind="price_change",
                question="Did 'Netflix' change price from $15.99 to $18.99?",
                payload={
                    "series_id": series.id,
                    "merchant": "Netflix",
                    "old_cents": -1599,
                    "new_cents": -1899,
                    "occurred_on": "2026-07-15",
                    "llm_flagged": True,
                },
            )
        )
        await session.commit()
        series_id = series.id

    items = (await client.get("/review")).json()
    item_id = items[0]["id"]
    assert items[0]["context"]["old_amount"] == "15.99"
    assert items[0]["context"]["new_amount"] == "18.99"

    r = await client.post(f"/review/{item_id}/resolve", json={"resolution": {}})
    assert r.status_code == 422

    r = await client.post(
        f"/review/{item_id}/resolve", json={"resolution": {"is_price_change": True}}
    )
    assert r.status_code == 200
    async with sessionmaker() as session:
        refreshed = (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.id == series_id)
            )
        ).scalar_one()
        assert refreshed.expected_cents == -1899
```

(Adjust imports in `tests/test_api.py`: `ReviewItem`, `RecurringSeries` from `moneta.models`, `select` from `sqlalchemy`, `make_series` from factories — check which are already present.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_autoreview.py tests/test_api.py -q`
Expected: FAIL — no context, no apply branch, no 422.

- [ ] **Step 3: Implement**

`src/moneta/pipelines/review.py` — add imports `date` (from `datetime`), `EventKind`, `SeriesEvent` (models import). In `review_context`, before the final `return {}`:

```python
    if item.kind == ReviewKind.price_change:
        old, new = item.payload.get("old_cents"), item.payload.get("new_cents")
        return {
            "merchant": item.payload.get("merchant"),
            "old_amount": f"{abs(old) / 100:.2f}" if isinstance(old, int) else None,
            "new_amount": f"{abs(new) / 100:.2f}" if isinstance(new, int) else None,
            "occurred_on": item.payload.get("occurred_on"),
        }
```

In `apply_resolution`, add alongside the other kind branches:

```python
    elif item.kind == ReviewKind.price_change and resolution.get("is_price_change") is True:
        series = (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.id == item.payload["series_id"])
            )
        ).scalar_one_or_none()
        if series is not None:
            session.add(
                SeriesEvent(
                    series_id=series.id,
                    kind=EventKind.price_increase,
                    occurred_on=date.fromisoformat(item.payload["occurred_on"]),
                    details={
                        "old_cents": series.expected_cents,
                        "new_cents": item.payload["new_cents"],
                    },
                )
            )
            series.expected_cents = item.payload["new_cents"]
```

`src/moneta/api.py` — in the resolve endpoint, after the recurring_cluster check:

```python
        if item.kind == ReviewKind.price_change and not isinstance(
            body.resolution.get("is_price_change"), bool
        ):
            raise HTTPException(status_code=422, detail="resolution.is_price_change must be a bool")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_autoreview.py tests/test_api.py -q`
Expected: all pass.

- [ ] **Step 5: Full gate, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/pipelines/review.py src/moneta/api.py tests/test_autoreview.py tests/test_api.py
git commit -m "feat: price_change review resolution applies the deferred amount"
```

---

### Task 7: CLI review UI for `price_change`

**Files:**
- Modify: `src/moneta/cli/main.py` (`_REVIEW_KINDS`, `_review_one`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `/review` context fields from Task 6 (`old_amount`, `new_amount`, `occurred_on`); resolve endpoint contract `{"is_price_change": bool}`.
- Produces: interactive y/n flow; shared `_prompt_yes_no` helper replacing the duplicated y/n parsing in the `recurring_cluster` branch.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (mirror `_seed_recurring_cluster_review`; `RecurringSeries` import needed — check header):

```python
def _seed_price_change_review(tmp_path: Path) -> None:
    async def _seed() -> None:
        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            series = RecurringSeries(
                merchant="Netflix",
                direction="outflow",
                cadence="monthly",
                expected_cents=-1599,
                next_expected_on=date(2026, 8, 1),
            )
            session.add(series)
            await session.flush()
            session.add(
                ReviewItem(
                    kind="price_change",
                    question="Did 'Netflix' change price from $15.99 to $18.99?",
                    payload={
                        "series_id": series.id,
                        "merchant": "Netflix",
                        "old_cents": -1599,
                        "new_cents": -1899,
                        "occurred_on": "2026-07-15",
                        "llm_flagged": True,
                    },
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())


def test_review_price_change_yes_resolves(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_price_change_review(tmp_path)
    result = runner.invoke(app, ["review"], input="y\n")
    assert result.exit_code == 0
    assert "$15.99 → $18.99" in result.output
    assert "Price change? [y/n]" in result.output
    assert "resolved" in result.output
    assert "Traceback" not in result.output


def test_review_price_change_invalid_answer_skips_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_price_change_review(tmp_path)
    result = runner.invoke(app, ["review"], input="maybe\n")
    assert result.exit_code == 0
    assert "invalid input, skipping" in result.output
    assert "Traceback" not in result.output
```

(`date` import needed in test file if absent; `RecurringSeries` from `moneta.models`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: new tests FAIL — unknown kind falls through `_review_one` (no prompt shown).

- [ ] **Step 3: Implement**

`src/moneta/cli/main.py` — add to `_REVIEW_KINDS`:

```python
    "price_change": (
        "price change",
        "confirming updates the expected amount behind `moneta power`",
    ),
```

Add the shared helper above `_review_one` and refactor the `recurring_cluster` branch to use it:

```python
def _prompt_yes_no(question: str) -> bool | None:
    answer = typer.prompt(question, default="", show_default=False)
    if not answer:
        return None
    normalized = answer.strip().lower()
    if normalized in ("y", "yes"):
        return True
    if normalized in ("n", "no"):
        return False
    console.print("[red]invalid input, skipping[/red]")
    return None
```

`recurring_cluster` branch becomes:

```python
    if item["kind"] == "recurring_cluster":
        for s in ctx.get("samples", []):
            console.print(f"    {s['posted_on']}  ${s['amount']}")
        if ctx.get("direction") == "inflow":
            console.print("    [dim](these are deposits — answering y counts them as income)[/dim]")
        answer = _prompt_yes_no("Recurring? [y/n]")
        return None if answer is None else {"is_recurring": answer}
```

New branch after it:

```python
    if item["kind"] == "price_change":
        console.print(
            f"    ${ctx.get('old_amount')} → ${ctx.get('new_amount')} on {ctx.get('occurred_on')}"
        )
        answer = _prompt_yes_no("Price change? [y/n]")
        return None if answer is None else {"is_price_change": answer}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: all pass, including the pre-existing recurring_cluster y/n tests (the refactor must not change their output).

- [ ] **Step 5: Full gate, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests
git add src/moneta/cli/main.py tests/test_cli.py
git commit -m "feat: CLI review flow for price_change items"
```
