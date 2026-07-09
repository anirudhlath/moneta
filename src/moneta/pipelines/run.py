from datetime import date, datetime, timedelta

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AggregatorAdapter
from moneta.llm import Classifier
from moneta.models import SyncRun, Transaction
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.ingest import IngestStats, ingest_snapshot
from moneta.pipelines.normalize import normalize_merchants
from moneta.pipelines.recurring import RecurringStats, detect_recurring
from moneta.pipelines.review import autoreview_items
from moneta.pipelines.transfers import TransferStats, link_transfers

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


class SyncReport(BaseModel):
    ingest: IngestStats
    normalized: int
    transfers: TransferStats
    auto_resolved: int
    recurring: RecurringStats
    events: int


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
