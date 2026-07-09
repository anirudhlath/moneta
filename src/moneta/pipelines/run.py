from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AggregatorAdapter
from moneta.llm import Classifier
from moneta.models import Transaction
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.ingest import IngestStats, ingest_snapshot
from moneta.pipelines.normalize import normalize_merchants
from moneta.pipelines.recurring import RecurringStats, detect_recurring
from moneta.pipelines.transfers import TransferStats, link_transfers

# SimpleFIN's default window when start-date is omitted is server-chosen (~1 day),
# so an explicit since is always sent: full history on first sync, overlap after.
FIRST_SYNC_DAYS = 90
RESYNC_OVERLAP_DAYS = 7


async def _sync_since(session: AsyncSession, today: date) -> date:
    newest = (await session.execute(select(func.max(Transaction.posted_on)))).scalar_one_or_none()
    if newest is None:
        return today - timedelta(days=FIRST_SYNC_DAYS)
    return newest - timedelta(days=RESYNC_OVERLAP_DAYS)


class SyncReport(BaseModel):
    ingest: IngestStats
    normalized: int
    transfers: TransferStats
    recurring: RecurringStats
    events: int


async def run_sync(
    session: AsyncSession,
    adapter: AggregatorAdapter,
    llm: Classifier | None,
    today: date,
) -> SyncReport:
    snap = await adapter.fetch(since=await _sync_since(session, today))
    ingest = await ingest_snapshot(session, snap)
    normalized = await normalize_merchants(session, llm)
    transfers = await link_transfers(session, llm)
    recurring = await detect_recurring(session, llm)
    events = await emit_series_events(session, today)
    return SyncReport(
        ingest=ingest,
        normalized=normalized,
        transfers=transfers,
        recurring=recurring,
        events=events,
    )
