from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO
from moneta.models import (
    Account,
    RecurringSeries,
    ReviewItem,
    SeriesEvent,
    SyncRun,
    Transaction,
    TransferLink,
)
from moneta.pipelines.run import RESYNC_OVERLAP_DAYS, run_sync
from tests.conftest import FakeAdapter, RecordingAdapter
from tests.factories import make_account, make_txn


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


async def test_run_sync_autoreview_resolves_before_detection(session: AsyncSession) -> None:
    from typing import Any

    from sqlalchemy import select

    from moneta.models import ReviewItem, ReviewStatus

    session.add(
        ReviewItem(
            kind="merchant",
            question="What merchant is 'Q9X8Z7Y6'?",
            payload={"descriptor": "Q9X8Z7Y6", "fallback": "Q9X8Z7Y6"},
        )
    )
    await session.flush()

    class ConfidentLLM:
        async def classify_json(self, prompt: str) -> dict[str, Any] | None:
            if "Q9X8Z7Y6" in prompt:
                return {"merchant": "Quix Labs", "confident": True}
            return None

    report = await run_sync(session, RecordingAdapter(), llm=ConfidentLLM(), today=date(2026, 7, 9))
    assert report.auto_resolved == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.resolved


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
    txn_count = (await session.execute(select(func.count()).select_from(Transaction))).scalar_one()
    assert txn_count == 0  # failed fetch leaves domain tables untouched


async def test_second_identical_sync_is_a_full_noop(session: AsyncSession) -> None:
    snap = Snapshot(
        accounts=[
            AccountDTO(
                id="A-1",
                name="Checking",
                org_name="Chase",
                currency="USD",
                balance=Decimal("1000.00"),
                balance_date=date(2026, 7, 1),
            )
        ],
        transactions=[
            TransactionDTO(
                id=f"T-{i}",
                account_id="A-1",
                posted_on=posted,
                amount=Decimal("-15.99"),
                description="NETFLIX.COM",
                raw={},
            )
            for i, posted in enumerate((date(2026, 4, 24), date(2026, 5, 24), date(2026, 6, 24)))
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
