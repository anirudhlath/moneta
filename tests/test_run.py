from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import Snapshot
from moneta.pipelines.run import FIRST_SYNC_DAYS, RESYNC_OVERLAP_DAYS, run_sync
from tests.factories import make_account, make_txn


class RecordingAdapter:
    """Records the `since` value run_sync passes to fetch."""

    def __init__(self) -> None:
        self.since: date | None | str = "never-called"

    async def fetch(self, since: date | None = None) -> Snapshot:
        self.since = since
        return Snapshot(accounts=[], transactions=[], holdings=[])


async def test_first_sync_requests_history_window(session: AsyncSession) -> None:
    adapter = RecordingAdapter()
    await run_sync(session, adapter, llm=None, today=date(2026, 7, 9))
    assert adapter.since == date(2026, 7, 9) - timedelta(days=FIRST_SYNC_DAYS)


async def test_resync_requests_from_newest_txn_with_overlap(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, posted_on=date(2026, 6, 1))
    await make_txn(session, acct, posted_on=date(2026, 7, 5))
    adapter = RecordingAdapter()
    await run_sync(session, adapter, llm=None, today=date(2026, 7, 9))
    assert adapter.since == date(2026, 7, 5) - timedelta(days=RESYNC_OVERLAP_DAYS)


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
