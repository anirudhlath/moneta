from datetime import date

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AggregatorAdapter
from moneta.llm import Classifier
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.ingest import IngestStats, ingest_snapshot
from moneta.pipelines.normalize import normalize_merchants
from moneta.pipelines.recurring import RecurringStats, detect_recurring
from moneta.pipelines.transfers import TransferStats, link_transfers


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
    snap = await adapter.fetch()
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
