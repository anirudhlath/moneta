from datetime import date, datetime, timedelta

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AggregatorAdapter, Snapshot, concat_snapshots
from moneta.llm import Classifier
from moneta.models import Account, SyncRun, Transaction
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.financing import detect_financing
from moneta.pipelines.ingest import IngestStats, ingest_snapshot
from moneta.pipelines.normalize import normalize_merchants
from moneta.pipelines.recurring import RecurringStats, detect_recurring
from moneta.pipelines.review import VerifyStats, autoreview_items, verify_series
from moneta.pipelines.transfers import TransferStats, link_transfers

# SimpleFIN's default window when start-date is omitted is server-chosen (~1 day), so an
# explicit since is always sent: the epoch on first/full sync (each institution returns
# whatever history it retains), overlap from the newest stored txn otherwise.
_EPOCH = date(1970, 1, 1)
RESYNC_OVERLAP_DAYS = 7


async def _sync_since(session: AsyncSession, full: bool, source: str) -> date:
    if full:
        return _EPOCH
    has_source = (
        await session.execute(
            select(func.count()).select_from(Account).where(Account.source == source)
        )
    ).scalar_one()
    if has_source:
        # this source's own newest txn — the per-source window that lets a
        # SimpleFIN outage self-heal without Plaid's daily replay masking it
        newest = (
            await session.execute(
                select(func.max(Transaction.posted_on))
                .join(Account, Transaction.account_id == Account.id)
                .where(Account.source == source)
            )
        ).scalar_one_or_none()
    else:
        # no accounts carry this source yet (first run after upgrading onto
        # source-attributed accounts) — fall back to the global max so a
        # known-but-not-yet-backfilled source doesn't re-pull full history.
        # NOTE: this also fires for a genuinely new source added to an already-
        # populated DB (its first-ever sync) — it inherits the global newest date
        # instead of the epoch, truncating its first pull; deliberate/accepted,
        # use `sync --full` when adding a new source.
        newest = (
            await session.execute(select(func.max(Transaction.posted_on)))
        ).scalar_one_or_none()
    if newest is None:
        return _EPOCH
    return newest - timedelta(days=RESYNC_OVERLAP_DAYS)


class SyncReport(BaseModel):
    ingest: IngestStats
    normalized: int
    transfers: TransferStats
    financing_questions: int
    auto_resolved: int
    recurring: RecurringStats
    verify: VerifyStats
    events: int
    warnings: list[str]


async def _fetch_all(
    session: AsyncSession, adapters: list[AggregatorAdapter], full: bool
) -> Snapshot:
    """Fetch every adapter with its own per-source `since`, merging into one snapshot.

    A failing adapter degrades to a warning and is skipped — UNLESS every configured
    adapter fails (the sole-adapter case is just the n=1 instance of this), in which
    case the first failure re-raises: an empty snapshot must never be ingested as a
    quiet success, or `data_as_of`/staleness would lie about having fresh data.
    """
    fetched: list[Snapshot] = []
    failures: list[tuple[str, Exception]] = []
    for adapter in adapters:
        since = await _sync_since(session, full, adapter.source)
        try:
            fetched.append(await adapter.fetch(since=since))
        except Exception as exc:
            logger.warning("sync: adapter {} failed: {}", adapter.source, exc)
            failures.append((adapter.source, exc))
    if failures and len(failures) == len(adapters):
        raise failures[0][1]
    snap = concat_snapshots(fetched)
    snap.warnings.extend(f"{source}: {type(exc).__name__}: {exc}" for source, exc in failures)
    return snap


async def run_sync(
    session: AsyncSession,
    adapters: list[AggregatorAdapter],
    llm: Classifier | None,
    today: date,
    full: bool = False,
) -> SyncReport:
    run = SyncRun()
    session.add(run)
    await session.commit()
    try:
        snap = await _fetch_all(session, adapters, full)
        ingest = await ingest_snapshot(session, snap)
        normalized = await normalize_merchants(session, llm)
        transfers = await link_transfers(session, llm)
        financing_questions = await detect_financing(session)
        # auto-review before detection so confident LLM answers (incl. last sync's
        # recurring questions) influence this run's series and exclusions
        auto_resolved = await autoreview_items(session, llm) if llm else 0
        recurring = await detect_recurring(session, llm, today)
        # second opinion on what detection produced, before events fire on it
        verify = await verify_series(session, llm)
        events = await emit_series_events(session, llm, today)
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
        financing_questions=financing_questions,
        auto_resolved=auto_resolved,
        recurring=recurring,
        verify=verify,
        events=events,
        warnings=snap.warnings,
    )
    run.finished_at = datetime.now()
    run.success = True
    run.report = report.model_dump(mode="json")
    await session.commit()
    logger.info("sync ok: {}", run.report)
    return report
