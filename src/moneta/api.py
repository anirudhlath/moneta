from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from moneta.aggregator.base import AggregatorAdapter
from moneta.aggregator.simplefin import SimpleFINAdapter
from moneta.config import load_settings
from moneta.db import init_db, make_sessionmaker
from moneta.llm import Classifier, build_classifier
from moneta.models import (
    Account,
    AccountType,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
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


class VestingIn(BaseModel):
    csv: str


class CashflowReport(BaseModel):
    start: date
    end: date
    accrual: Decimal
    cash_out: Decimal


def create_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    adapter: AggregatorAdapter | None,
    llm: Classifier | None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if engine is not None:
            await init_db(engine)
        yield

    app = FastAPI(title="moneta", lifespan=lifespan)

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
        series.status = body.status
        await session.commit()
        return {"ok": True}

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
    )
