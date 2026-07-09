import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from moneta.aggregator.base import AggregatorAdapter
from moneta.aggregator.simplefin import SimpleFINAdapter
from moneta.config import load_settings
from moneta.db import init_db, make_sessionmaker
from moneta.llm import Classifier, build_classifier
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
)
from moneta.pipelines.normalize import renormalize_merchants
from moneta.pipelines.review import apply_resolution, review_context
from moneta.pipelines.run import SyncReport, run_sync
from moneta.queries import classified_links
from moneta.vesting import apply_vesting, parse_vesting_csv
from moneta.views.cashflow import accrual_spend, cash_out
from moneta.views.financing import Obligation, compute_obligations
from moneta.views.networth import NetWorthReport, net_worth_report
from moneta.views.power import PowerReport, power_report


class AccountOut(BaseModel):
    id: int
    name: str
    org_name: str
    type: AccountType
    balance: str
    promo_expires_on: date | None


class AccountPatch(BaseModel):
    type: AccountType | None = None
    promo_expires_on: date | None = None


class SeriesOut(BaseModel):
    id: int
    merchant: str
    direction: str
    cadence: str
    expected_cents: int
    expected_amount: str
    next_expected_on: date
    status: str


class SeriesPatch(BaseModel):
    status: SeriesStatus


class EventOut(BaseModel):
    id: int
    series_id: int
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


class VestingIn(BaseModel):
    csv: str


class CashflowReport(BaseModel):
    start: date
    end: date
    accrual: Decimal
    cash_out: Decimal


class SyncRunOut(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    success: bool
    error: str | None
    report: dict[str, Any] | None


class BackupIn(BaseModel):
    dest: str | None = None


def create_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    adapter: AggregatorAdapter | None,
    llm: Classifier | None,
    engine: AsyncEngine | None = None,
    api_token: str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if engine is not None:
            await init_db(engine)
        yield

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

    @app.post("/sync")
    async def sync(session: Session, full: bool = False) -> SyncReport:
        if adapter is None:
            raise HTTPException(
                status_code=400,
                detail="No SimpleFIN aggregator configured. Run: moneta setup simplefin <token>",
            )
        return await run_sync(session, adapter, llm, today=date.today(), full=full)

    @app.get("/sync/last")
    async def sync_last(session: Session) -> SyncRunOut | None:
        row = (
            await session.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
        ).scalar_one_or_none()
        return SyncRunOut.model_validate(row, from_attributes=True) if row else None

    @app.get("/power")
    async def power(session: Session) -> PowerReport:
        return await power_report(session, today=date.today())

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
        return CashflowReport(
            start=range_start,
            end=range_end,
            accrual=await accrual_spend(session, range_start, range_end, links=links),
            cash_out=await cash_out(session, range_start, range_end, links=links),
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
                expected_amount=f"{abs(r.expected_cents) / 100:.2f}",
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
            # forward-only bump: reactivating must not resurrect ancient missed windows
            series.next_expected_on = max(series.next_expected_on, date.today())
        series.status = body.status
        await session.commit()
        return {"ok": True}

    @app.get("/recurring/events")
    async def events(session: Session) -> list[EventOut]:
        rows = (
            (await session.execute(select(SeriesEvent).order_by(SeriesEvent.occurred_on.desc())))
            .scalars()
            .all()
        )
        return [EventOut.model_validate(r, from_attributes=True) for r in rows]

    @app.get("/accounts")
    async def accounts(session: Session) -> list[AccountOut]:
        rows = (await session.execute(select(Account))).scalars().all()
        return [
            AccountOut(
                id=a.id,
                name=a.name,
                org_name=a.org_name,
                type=a.type,
                balance=f"{a.balance_cents / 100:.2f}",
                promo_expires_on=a.promo_expires_on,
            )
            for a in rows
        ]

    @app.patch("/accounts/{account_id}")
    async def patch_account(
        account_id: int, body: AccountPatch, session: Session
    ) -> dict[str, bool]:
        acct = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        if body.type is not None:
            acct.type = body.type
        if "promo_expires_on" in body.model_fields_set:
            acct.promo_expires_on = body.promo_expires_on
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
        if item.kind == ReviewKind.recurring_cluster and not isinstance(
            body.resolution.get("is_recurring"), bool
        ):
            raise HTTPException(status_code=422, detail="resolution.is_recurring must be a bool")
        await apply_resolution(session, item, body.resolution, resolved_by="manual")
        await session.commit()
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
        async with engine.connect() as conn:
            ac = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await ac.exec_driver_sql("VACUUM INTO ?", (str(dest),))
        dest.chmod(0o600)  # the backup is the full financial DB
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


def build_app() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.config_dir)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{settings.db_path}")
    adapter: AggregatorAdapter | None = (
        SimpleFINAdapter(settings.simplefin_access_url) if settings.simplefin_access_url else None
    )
    return create_app(
        sessionmaker,
        adapter,
        build_classifier(settings.llm_model),
        engine=engine,
        api_token=settings.api_token,
    )
