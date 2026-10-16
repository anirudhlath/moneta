from datetime import date, datetime, timedelta

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AggregatorAdapter, Snapshot, concat_snapshots
from moneta.llm import Classifier
from moneta.models import Account, SyncRun, Transaction
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.financing import detect_financing
from moneta.pipelines.ingest import IngestStats, ingest_snapshot
from moneta.pipelines.normalize import normalize_merchants, renormalize_merchants
from moneta.pipelines.reconcile import OverlapStats, reconcile_provider_overlap
from moneta.pipelines.recurring import RecurringStats, detect_recurring
from moneta.pipelines.review import VerifyStats, autoreview_items, verify_series
from moneta.pipelines.transfers import TransferStats, link_transfers

# SimpleFIN's default window when start-date is omitted is server-chosen (~1 day), so an
# explicit since is always sent: the epoch on first/full sync (each institution returns
# whatever history it retains), otherwise the newest stored txn of the source's
# most-behind active account minus the overlap (see _sync_since).
_EPOCH = date(1970, 1, 1)
RESYNC_OVERLAP_DAYS = 7
# An account whose newest txn sits more than this many days behind its source's
# newest txn is dormant (closed, paid off): it no longer holds the window back.
ACTIVE_ACCOUNT_DAYS = 90


async def _sync_since(session: AsyncSession, full: bool, source: str) -> date:
    if full:
        return _EPOCH
    has_source = (
        await session.execute(
            select(func.count()).select_from(Account).where(Account.source == source)
        )
    ).scalar_one()
    newest: date | None
    if has_source:
        # Resume from this source's most-behind ACTIVE account, never from the source's
        # newest txn: a card that delivers a month of back-dated rows at statement time
        # must not lose them behind a sibling account that posted yesterday. Each
        # account's newest physical observation counts, suppressed duplicates included;
        # an account with no transactions has no row here and never moves the window.
        per_account = list(
            await session.scalars(
                select(func.max(Transaction.posted_on))
                .join(Account, Transaction.account_id == Account.id)
                .where(Account.source == source)
                .group_by(Transaction.account_id)
            )
        )
        # dormancy is measured from the source's own newest txn, not today, so a whole
        # source stalled for months still resumes from its most-behind account
        horizon = max(per_account, default=_EPOCH) - timedelta(days=ACTIVE_ACCOUNT_DAYS)
        newest = min((d for d in per_account if d >= horizon), default=None)
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
    overlap: OverlapStats = Field(default_factory=OverlapStats)
    ingest: IngestStats
    normalized: int
    merchants_renamed: int = 0
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
    # an empty adapter list must be rejected by callers (/sync 400s); a silent
    # empty-snapshot "success" here would stamp data_as_of and lie about freshness
    if not adapters:
        raise ValueError("run_sync requires at least one adapter")
    run = SyncRun()
    session.add(run)
    await session.commit()
    try:
        snap = await _fetch_all(session, adapters, full)
        ingest = await ingest_snapshot(session, snap)
        normalized = await normalize_merchants(session, llm)
        # every sync: improved rules and name merges reach existing aliases, series
        # and decisions, so one bill's descriptors form one merchant (design 2026-10-13 §1)
        merchants_renamed = await renormalize_merchants(session)
        overlap = await reconcile_provider_overlap(session, today)
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
        overlap=overlap,
        ingest=ingest,
        normalized=normalized,
        merchants_renamed=merchants_renamed,
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
