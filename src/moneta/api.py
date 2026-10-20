import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, StrictInt
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from moneta.aggregator.base import AggregatorAdapter
from moneta.aggregator.plaid import PlaidAdapter, PlaidClient, items_path, load_items
from moneta.aggregator.simplefin import SimpleFINAdapter
from moneta.cadence import month_bounds
from moneta.config import Settings, load_settings, make_private
from moneta.db import init_db, make_sessionmaker
from moneta.llm import Classifier, build_classifier
from moneta.logical import load_logical_data
from moneta.logs import configure_logging
from moneta.models import (
    Account,
    AccountType,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
    SyncRun,
    TransferLink,
    amounts_match,
    bill_ref,
    recurring_cluster_item,
)
from moneta.pipelines.digest import DigestResult, run_digest
from moneta.pipelines.normalize import renormalize_merchants
from moneta.pipelines.reconcile import (
    OverlapStats,
    rebuild_logical_derived,
    reconcile_provider_overlap,
)
from moneta.pipelines.recurring import reactivate_series, rule_pass_through
from moneta.pipelines.review import apply_resolution, review_context
from moneta.pipelines.run import SyncReport, run_sync
from moneta.queries import (
    StaleAccount,
    bill_amount,
    classified_links,
    primary_currency,
    stale_accounts,
)
from moneta.vesting import apply_vesting, parse_vesting_csv
from moneta.views.cashflow import accrual_by_month, accrual_spend, cash_out
from moneta.views.financing import Obligation, compute_obligations
from moneta.views.networth import NetWorthReport, net_worth_report
from moneta.views.power import PowerReport, power_report
from moneta.views.transactions import TxnRow, spend_totals, transactions_report


class AccountOut(BaseModel):
    id: int
    name: str
    org_name: str
    type: AccountType
    balance_cents: int
    promo_expires_on: date | None
    financing_mode: bool


class AccountPatch(BaseModel):
    type: AccountType | None = None
    promo_expires_on: date | None = None
    financing_mode: bool | None = None


class PassThroughRuling(BaseModel):
    merchant: str
    pass_through: bool


class SourceAccountOut(AccountOut):
    source: str
    mask: str | None
    canonical_account_id: int | None


class AccountLinkIn(BaseModel):
    canonical_account_id: StrictInt


class SeriesOut(BaseModel):
    id: int
    merchant: str
    direction: str
    cadence: str
    expected_cents: int
    next_expected_on: date
    status: str


class SeriesPatch(BaseModel):
    status: SeriesStatus


class EventOut(BaseModel):
    id: int
    series_id: int
    merchant: str
    kind: str
    occurred_on: date
    details: dict[str, Any]


class ReviewOut(BaseModel):
    id: int
    kind: str
    question: str
    payload: dict[str, Any]
    context: dict[str, Any] = {}


class ResolveIn(BaseModel):
    resolution: dict[str, Any]


# review kinds whose resolution must carry a boolean answer under this key
_REQUIRED_BOOL: dict[ReviewKind, str] = {
    ReviewKind.recurring_cluster: "is_recurring",
    ReviewKind.price_change: "is_price_change",
    ReviewKind.financing_account: "financing",
    ReviewKind.pass_through: "pass_through",
}


class VestingIn(BaseModel):
    csv: str


class CashflowReport(BaseModel):
    start: date
    end: date
    accrual_cents: int
    cash_out_cents: int


class PowerMonth(BaseModel):
    month: str  # "2026-07"
    income_cents: int  # magnitude: inflows tagged to an income series (active or ended)
    other_inflows_cents: int  # magnitude: every other counted inflow
    spend_cents: int  # magnitude
    net_cents: int  # signed: income - spend


class TransactionsReport(BaseModel):
    start: date
    end: date
    counted_total_cents: int  # magnitude, sum of every counted row regardless of date
    through_today_cents: int | None  # magnitude through today; None if range excludes today
    transactions: list[TxnRow]


class StatusOut(BaseModel):
    last_sync_at: datetime | None
    last_sync_ok: bool | None
    accounts: int
    open_reviews: int
    aggregators: list[str]
    llm_configured: bool
    stale_accounts: list[StaleAccount]


class SyncRunOut(BaseModel):
    status: Literal["ok", "failed", "incomplete"]
    started_at: datetime
    finished_at: datetime | None
    success: bool
    error: str | None
    report: dict[str, Any] | None


class BackupIn(BaseModel):
    dest: str | None = None


async def _latest_sync_run(session: AsyncSession) -> SyncRun | None:
    """The newest `SyncRun` row, or None before any sync has run. Shared by
    /sync/last and /status so the "most recent run" query lives in one place."""
    return (
        await session.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
    ).scalar_one_or_none()


def create_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    adapters: list[AggregatorAdapter],
    llm: Classifier | None,
    engine: AsyncEngine | None = None,
    api_token: str | None = None,
    ntfy_topic: str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if engine is not None:
            await init_db(engine)
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    async def check_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if api_token is None:
            return
        expected = f"Bearer {api_token}"
        if authorization is None or not secrets.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    # app-level dependencies don't guard /docs//openapi.json — disable them when locked
    public = api_token is None
    app = FastAPI(
        title="moneta",
        lifespan=lifespan,
        dependencies=[Depends(check_auth)],
        docs_url="/docs" if public else None,
        redoc_url="/redoc" if public else None,
        openapi_url="/openapi.json" if public else None,
    )

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    Session = Annotated[AsyncSession, Depends(get_session)]

    async def apply_manual(
        session: AsyncSession, item: ReviewItem, resolution: dict[str, Any]
    ) -> None:
        try:
            await apply_resolution(session, item, resolution, resolved_by="manual")
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def rebuild_overlap(session: AsyncSession) -> OverlapStats:
        try:
            stats = await reconcile_provider_overlap(session, date.today())
            await rebuild_logical_derived(session, None, date.today())
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return stats

    @app.post("/reconcile")
    async def reconcile(session: Session) -> OverlapStats:
        return await rebuild_overlap(session)

    @app.post("/sync")
    async def sync(session: Session, full: bool = False) -> SyncReport:
        if not adapters:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No aggregator configured. Connect one with: "
                    "moneta setup simplefin <token> or moneta setup plaid <client_id> <secret>"
                ),
            )
        return await run_sync(session, adapters, llm, today=date.today(), full=full)

    @app.post("/digest")
    async def digest(session: Session) -> DigestResult:
        if not ntfy_topic:
            raise HTTPException(
                status_code=400,
                detail=(
                    "ntfy_topic not configured. Set MONETA_NTFY_TOPIC or ntfy_topic in "
                    "config.toml to a topic URL (get one free at https://ntfy.sh)."
                ),
            )
        return await run_digest(session, ntfy_topic, today=date.today())

    @app.get("/sync/last")
    async def sync_last(session: Session) -> SyncRunOut | None:
        row = await _latest_sync_run(session)
        if row is None:
            return None
        # the outcome rule lives here, not in each consumer: an unfinished row means
        # the sync is still running or the process died mid-sync
        if row.finished_at is None:
            status: Literal["ok", "failed", "incomplete"] = "incomplete"
        else:
            status = "ok" if row.success else "failed"
        return SyncRunOut(
            status=status,
            started_at=row.started_at,
            finished_at=row.finished_at,
            success=row.success,
            error=row.error,
            report=row.report,
        )

    @app.get("/status")
    async def status(session: Session) -> StatusOut:
        """Overall health: booleans, counts and per-account coverage notes — never
        secrets (design 2026-07-16 §3)."""
        last_run = await _latest_sync_run(session)
        last_sync_at = last_run.finished_at if last_run is not None else None
        # last_sync_ok is unknown (None), not False, while the newest run is still in
        # flight — a bare boolean would misreport an unfinished sync as a failure
        last_sync_ok = (
            last_run.success if last_run is not None and last_run.finished_at is not None else None
        )
        logical = await load_logical_data(session)
        open_reviews_count = (
            await session.execute(
                select(func.count())
                .select_from(ReviewItem)
                .where(ReviewItem.status == ReviewStatus.open)
            )
        ).scalar_one()
        return StatusOut(
            last_sync_at=last_sync_at,
            last_sync_ok=last_sync_ok,
            accounts=len(logical.accounts),
            open_reviews=open_reviews_count,
            aggregators=[a.source for a in adapters],
            llm_configured=llm is not None,
            stale_accounts=await stale_accounts(session, logical, date.today()),
        )

    @app.get("/power")
    async def power(session: Session) -> PowerReport:
        return await power_report(session, today=date.today())

    @app.get("/power/history")
    async def power_history(
        session: Session, months: Annotated[int, Query(ge=1, le=60)] = 6
    ) -> list[PowerMonth]:
        """Month-over-month income/other inflows/spend, newest first (the newest row is
        the current, still-in-progress month). Unlike GET /power, which reports today's
        recurring-series state, each month here reports the *observed* money for that
        calendar month, so a since-cancelled bill or a past raise still shows correctly.
        Today's series tags only decide the split of inflows: income is inflows tagged to
        an income series (non-discretionary inflow series, active or ended), so marking a
        paycheck series a habit today moves its past paychecks to other_inflows_cents.
        Every other counted inflow is other_inflows_cents, and net is income - spend."""
        today = date.today()
        links = await classified_links(session)
        primary = await primary_currency(session)
        rows = await accrual_by_month(session, months, today, links=links, primary=primary)
        return [
            PowerMonth(
                month=row.month,
                income_cents=row.income_cents,
                other_inflows_cents=row.other_inflows_cents,
                spend_cents=row.spend_cents,
                net_cents=row.income_cents - row.spend_cents,
            )
            for row in rows
        ]

    @app.get("/networth")
    async def networth(session: Session) -> NetWorthReport:
        return await net_worth_report(session)

    @app.get("/obligations")
    async def obligations(session: Session) -> list[Obligation]:
        return await compute_obligations(session, today=date.today())

    @app.get("/cashflow")
    async def cashflow(
        session: Session, start: date | None = None, end: date | None = None
    ) -> CashflowReport:
        today = date.today()
        range_start = start or today.replace(day=1)
        range_end = end or today
        links = await classified_links(session)
        primary = await primary_currency(session)
        return CashflowReport(
            start=range_start,
            end=range_end,
            accrual_cents=await accrual_spend(
                session, range_start, range_end, links=links, primary=primary
            ),
            cash_out_cents=await cash_out(
                session, range_start, range_end, links=links, primary=primary
            ),
        )

    @app.get("/transactions")
    async def transactions(
        session: Session,
        start: date | None = None,
        end: date | None = None,
        account_id: int | None = None,
        merchant: str | None = None,
        sources: bool = False,
    ) -> TransactionsReport:
        """Trust/audit view: every transaction in range, with why it is or isn't
        counted as spend, plus server-computed totals. Defaults to the current
        calendar month."""
        today = date.today()
        default_start, default_end = month_bounds(today)
        range_start = start or default_start
        range_end = end or default_end
        rows = await transactions_report(
            session,
            range_start,
            range_end,
            account_id=account_id,
            merchant=merchant,
            include_sources=sources,
        )
        counted_total, through_today = spend_totals(rows, range_start, range_end, today)
        return TransactionsReport(
            start=range_start,
            end=range_end,
            counted_total_cents=counted_total,
            through_today_cents=through_today,
            transactions=rows,
        )

    @app.get("/recurring")
    async def recurring(session: Session) -> list[SeriesOut]:
        rows = (await session.execute(select(RecurringSeries))).scalars().all()
        return [
            SeriesOut(
                id=r.id,
                merchant=r.merchant,
                direction=r.direction,
                cadence=r.cadence,
                expected_cents=r.expected_cents,
                next_expected_on=r.next_expected_on,
                status=r.status,
            )
            for r in rows
        ]

    @app.patch("/recurring/{series_id}")
    async def patch_recurring(
        series_id: int, body: SeriesPatch, session: Session
    ) -> dict[str, bool]:
        series = (
            await session.execute(select(RecurringSeries).where(RecurringSeries.id == series_id))
        ).scalar_one_or_none()
        if series is None:
            raise HTTPException(status_code=404, detail="series not found")
        if body.status == SeriesStatus.active and series.status != SeriesStatus.active:
            reactivate_series(series, today=date.today())
        else:
            series.status = body.status
        await session.commit()
        return {"ok": True}

    async def _series_ledger_item(
        session: AsyncSession, series_id: int
    ) -> tuple[RecurringSeries, ReviewItem]:
        series = (
            await session.execute(select(RecurringSeries).where(RecurringSeries.id == series_id))
        ).scalar_one_or_none()
        if series is None:
            raise HTTPException(status_code=404, detail="series not found")
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

    @app.post("/recurring/{series_id}/not-a-bill")
    async def not_a_bill(series_id: int, session: Session) -> dict[str, bool]:
        _series, item = await _series_ledger_item(session, series_id)
        await apply_resolution(session, item, {"is_recurring": False}, resolved_by="manual")
        await session.commit()
        return {"ok": True}

    @app.post("/recurring/{series_id}/habit")
    async def mark_habit(series_id: int, session: Session) -> dict[str, bool]:
        series, item = await _series_ledger_item(session, series_id)
        await apply_resolution(
            session, item, {"is_recurring": True, "discretionary": True}, resolved_by="manual"
        )
        if series.status != SeriesStatus.active:
            reactivate_series(series, today=date.today())
        await session.commit()
        return {"ok": True}

    @app.post("/recurring/{series_id}/re-review")
    async def re_review(series_id: int, session: Session) -> dict[str, bool]:
        _series, item = await _series_ledger_item(session, series_id)
        item.status = ReviewStatus.open
        item.resolution = None
        await session.commit()
        return {"ok": True}

    @app.post("/merchants/pass-through")
    async def set_pass_through(body: PassThroughRuling, session: Session) -> PassThroughRuling:
        """Rule a merchant pass-through (someone else's money moving through the
        account) or clear the ruling. The name matches the merchants `GET
        /transactions` shows (logical survivors), ignoring case and surrounding spaces."""
        wanted = body.merchant.strip()
        logical = await load_logical_data(session)
        names = sorted(
            {
                t.merchant
                for t in logical.transactions
                if t.merchant is not None and t.merchant.casefold() == wanted.casefold()
            }
        )
        if not names:
            raise HTTPException(status_code=404, detail=f"no transactions from {wanted!r}")
        await rule_pass_through(session, names, body.pass_through, date.today())
        await session.commit()
        return PassThroughRuling(merchant=names[0], pass_through=body.pass_through)

    @app.get("/recurring/events")
    async def events(session: Session) -> list[EventOut]:
        rows = (
            await session.execute(
                select(SeriesEvent, RecurringSeries.merchant)
                .outerjoin(RecurringSeries, SeriesEvent.series_id == RecurringSeries.id)
                .order_by(SeriesEvent.occurred_on.desc())
            )
        ).all()
        return [
            EventOut(
                id=e.id,
                series_id=e.series_id,
                # outer join: an orphaned event (SQLite doesn't enforce FKs) must still surface
                merchant=merchant or f"series {e.series_id}",
                kind=e.kind,
                occurred_on=e.occurred_on,
                details=e.details,
            )
            for e, merchant in rows
        ]

    @app.get("/accounts")
    async def accounts(
        session: Session, sources: bool = False
    ) -> list[AccountOut | SourceAccountOut]:
        if sources:
            physical = list(await session.scalars(select(Account).order_by(Account.id)))
            return [
                SourceAccountOut(
                    id=a.id,
                    name=a.name,
                    org_name=a.org_name,
                    type=a.type,
                    balance_cents=a.balance_cents,
                    promo_expires_on=a.promo_expires_on,
                    financing_mode=a.financing_mode,
                    source=a.source,
                    mask=a.mask,
                    canonical_account_id=a.canonical_account_id,
                )
                for a in physical
            ]
        rows = (await load_logical_data(session)).accounts.values()
        return [
            AccountOut(
                id=a.id,
                name=a.name,
                org_name=a.org_name,
                type=a.type,
                balance_cents=a.balance_cents,
                promo_expires_on=a.promo_expires_on,
                financing_mode=a.financing_mode,
            )
            for a in rows
        ]

    async def account_identity(
        account_id: int, target: int | None, session: AsyncSession
    ) -> dict[str, bool]:
        if await session.get(Account, account_id) is None:
            raise HTTPException(status_code=404, detail="account not found")
        item = ReviewItem(
            kind=ReviewKind.account_overlap,
            question="Explicit account identity decision",
            payload={"account_id": account_id, "candidates": [] if target is None else [target]},
        )
        await apply_manual(session, item, {"canonical_account_id": target})
        await session.commit()
        await rebuild_overlap(session)
        return {"ok": True}

    @app.post("/accounts/{account_id}/link")
    async def link_account(
        account_id: int, body: AccountLinkIn, session: Session
    ) -> dict[str, bool]:
        return await account_identity(account_id, body.canonical_account_id, session)

    @app.post("/accounts/{account_id}/unlink")
    async def unlink_account(account_id: int, session: Session) -> dict[str, bool]:
        return await account_identity(account_id, None, session)

    @app.patch("/accounts/{account_id}")
    async def patch_account(
        account_id: int, body: AccountPatch, session: Session
    ) -> dict[str, bool]:
        logical = await load_logical_data(session)
        acct = await session.get(Account, logical.account_roots.get(account_id, account_id))
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        if body.type is not None:
            acct.type = body.type
        if "promo_expires_on" in body.model_fields_set:
            acct.promo_expires_on = body.promo_expires_on
            session.add(
                ReviewItem(
                    kind=ReviewKind.financing_account,
                    question="Explicit promotion metadata decision",
                    payload={"account_id": acct.id, "metadata_only": True},
                    status=ReviewStatus.resolved,
                    resolution={
                        "promo_expires_on": (
                            body.promo_expires_on.isoformat() if body.promo_expires_on else None
                        ),
                        "resolved_by": "manual",
                    },
                )
            )
        if "financing_mode" in body.model_fields_set and body.financing_mode is not None:
            resolution = {"financing": body.financing_mode}
            for item in await session.scalars(
                select(ReviewItem).where(
                    ReviewItem.kind == ReviewKind.financing_account,
                    ReviewItem.status == ReviewStatus.open,
                )
            ):
                review_account_id = item.payload.get("account_id")
                if (
                    type(review_account_id) is int
                    and logical.account_roots.get(review_account_id) == acct.id
                ):
                    await apply_manual(session, item, resolution)
            item = ReviewItem(
                kind=ReviewKind.financing_account,
                question="Explicit financing decision",
                payload={"account_id": acct.id},
            )
            session.add(item)
            await apply_manual(session, item, resolution)
        await session.commit()
        return {"ok": True}

    @app.get("/review")
    async def review(session: Session) -> list[ReviewOut]:
        rows = (
            (
                await session.execute(
                    select(ReviewItem).where(ReviewItem.status == ReviewStatus.open)
                )
            )
            .scalars()
            .all()
        )
        return [
            ReviewOut(
                id=r.id,
                kind=r.kind,
                question=r.question,
                payload=r.payload,
                context=await review_context(session, r),
            )
            for r in rows
        ]

    @app.post("/review/{item_id}/resolve")
    async def resolve(item_id: int, body: ResolveIn, session: Session) -> dict[str, bool]:
        item = (
            await session.execute(select(ReviewItem).where(ReviewItem.id == item_id))
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="review item not found")
        required = _REQUIRED_BOOL.get(item.kind)
        if required is not None and not isinstance(body.resolution.get(required), bool):
            raise HTTPException(status_code=422, detail=f"resolution.{required} must be a bool")
        if item.kind == ReviewKind.transfer_pair:
            outflow_id = item.payload.get("outflow_id")
            inflow_id = body.resolution.get("inflow_id")
            roots = (await load_logical_data(session)).transaction_roots
            legs = []
            if type(outflow_id) is int and outflow_id in roots:
                legs.append(
                    TransferLink.outflow_id.in_(
                        [row_id for row_id, root in roots.items() if root == roots[outflow_id]]
                    )
                )
            if type(inflow_id) is int and inflow_id in roots:
                legs.append(
                    TransferLink.inflow_id.in_(
                        [row_id for row_id, root in roots.items() if root == roots[inflow_id]]
                    )
                )
            # .first(), not scalar_one_or_none(): each leg can already be linked in a
            # SEPARATE row (e.g. a greedy-loser item resolved after both legs were
            # linked elsewhere) — two matches must still 409, not MultipleResultsFound.
            existing_link = (
                (await session.execute(select(TransferLink).where(or_(*legs)))).scalars().first()
                if legs
                else None
            )
            if existing_link is not None:
                raise HTTPException(status_code=409, detail="transaction already linked")
        await apply_manual(session, item, body.resolution)
        await session.commit()
        if item.kind in (ReviewKind.account_overlap, ReviewKind.transaction_overlap):
            await rebuild_overlap(session)
        return {"ok": True}

    @app.post("/backup")
    async def backup(body: BackupIn) -> dict[str, str]:
        db_file = engine.url.database if engine is not None else None
        if engine is None or not db_file or db_file == ":memory:":
            raise HTTPException(status_code=400, detail="backup requires a file-backed database")
        dest = (
            Path(body.dest).expanduser()
            if body.dest
            else Path(db_file).with_name(f"moneta-backup-{datetime.now():%Y%m%d-%H%M%S}.db")
        )
        if dest.exists():
            raise HTTPException(status_code=409, detail=f"destination already exists: {dest}")
        old_umask = os.umask(0o077)  # VACUUM INTO creates dest itself — never world-readable
        try:
            async with engine.connect() as conn:
                ac = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await ac.exec_driver_sql("VACUUM INTO ?", (str(dest),))
        finally:
            os.umask(old_umask)
        make_private(dest)  # the backup is the full financial DB
        return {"path": str(dest)}

    @app.post("/normalize/rerun")
    async def normalize_rerun(session: Session) -> dict[str, int]:
        return {"changed": await renormalize_merchants(session)}

    @app.post("/import/vesting")
    async def import_vesting(body: VestingIn, session: Session) -> dict[str, int]:
        try:
            rows = parse_vesting_csv(body.csv)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"updated": await apply_vesting(session, rows)}

    return app


def _build_adapters(settings: Settings) -> list[AggregatorAdapter]:
    adapters: list[AggregatorAdapter] = []
    if settings.simplefin_access_url:
        adapters.append(SimpleFINAdapter(settings.simplefin_access_url))
    if settings.plaid_client_id and settings.plaid_secret:
        try:
            items = load_items(items_path(settings.config_dir))
        except ValueError as exc:
            # a corrupt items file must not take down every endpoint — sync just
            # runs without Plaid until the user re-links
            logger.warning("{}", exc)
            items = []
        if items:
            adapters.append(
                PlaidAdapter(
                    PlaidClient(
                        settings.plaid_client_id, settings.plaid_secret, settings.plaid_env
                    ),
                    items,
                )
            )
    return adapters


def build_app() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.config_dir)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{settings.db_path}")
    return create_app(
        sessionmaker,
        _build_adapters(settings),
        build_classifier(settings.llm_model, settings.llm_api_base),
        engine=engine,
        api_token=settings.api_token,
        ntfy_topic=settings.ntfy_topic,
    )
