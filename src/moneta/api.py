from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
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
    MerchantAlias,
    RecurringSeries,
    ReviewItem,
    ReviewStatus,
    SeriesEvent,
    SeriesStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.run import SyncReport, run_sync
from moneta.vesting import apply_vesting, parse_vesting_csv
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


class ResolveIn(BaseModel):
    resolution: dict[str, Any]


class VestingIn(BaseModel):
    csv: str


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
    async def sync(session: Session) -> SyncReport:
        if adapter is None:
            raise HTTPException(
                status_code=400,
                detail="No SimpleFIN aggregator configured. Run: moneta setup simplefin <token>",
            )
        return await run_sync(session, adapter, llm, today=date.today())

    @app.get("/power")
    async def power(session: Session) -> PowerReport:
        return await power_report(session, today=date.today())

    @app.get("/networth")
    async def networth(session: Session) -> NetWorthReport:
        return await net_worth_report(session)

    @app.get("/obligations")
    async def obligations(session: Session) -> list[Obligation]:
        return await compute_obligations(session, today=date.today())

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
        return [ReviewOut.model_validate(r, from_attributes=True) for r in rows]

    @app.post("/review/{item_id}/resolve")
    async def resolve(item_id: int, body: ResolveIn, session: Session) -> dict[str, bool]:
        item = (
            await session.execute(select(ReviewItem).where(ReviewItem.id == item_id))
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="review item not found")
        if item.kind == "merchant" and isinstance(body.resolution.get("merchant"), str):
            merchant = body.resolution["merchant"]
            raw = item.payload["descriptor"]
            alias = (
                await session.execute(
                    select(MerchantAlias).where(MerchantAlias.raw_descriptor == raw)
                )
            ).scalar_one_or_none()
            if alias is None:
                session.add(MerchantAlias(raw_descriptor=raw, merchant=merchant, source="manual"))
            else:
                alias.merchant = merchant
                alias.source = "manual"
            for txn in (
                await session.execute(select(Transaction).where(Transaction.description == raw))
            ).scalars():
                txn.merchant = merchant
        elif item.kind == "transfer_pair" and isinstance(body.resolution.get("inflow_id"), int):
            session.add(
                TransferLink(
                    outflow_id=item.payload["outflow_id"],
                    inflow_id=body.resolution["inflow_id"],
                    confidence=1.0,
                    method="manual",
                )
            )
        item.status = ReviewStatus.resolved
        item.resolution = body.resolution
        await session.commit()
        return {"ok": True}

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
