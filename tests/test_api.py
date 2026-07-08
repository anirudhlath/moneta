from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO
from moneta.api import create_app
from moneta.pipelines.recurring import detect_recurring
from tests.conftest import FakeAdapter
from tests.factories import make_account, make_txn

SNAP = Snapshot(
    accounts=[
        AccountDTO(
            id="ACT-1",
            name="Premier Checking",
            org_name="Chase",
            currency="USD",
            balance=Decimal("1000.00"),
            balance_date=date(2026, 7, 1),
        ),
    ],
    transactions=[
        TransactionDTO(
            id=f"TRN-{m}",
            account_id="ACT-1",
            posted_on=date(2026, m, 15),
            amount=Decimal("-15.99"),
            description="NETFLIX.COM",
            raw={},
        )
        for m in (4, 5, 6)
    ],
    holdings=[],
)


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(sessionmaker, adapter=FakeAdapter(SNAP), llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_sync_then_views(client: httpx.AsyncClient) -> None:
    r = await client.post("/sync")
    assert r.status_code == 200
    body = r.json()
    assert body["ingest"]["new_transactions"] == 3
    assert body["recurring"]["new_series"] == 1

    r = await client.get("/power")
    assert r.status_code == 200
    assert Decimal(r.json()["total_fixed"]) == Decimal("15.99")

    r = await client.get("/recurring")
    assert r.json()[0]["merchant"] == "Netflix.Com"

    r = await client.get("/networth")
    assert Decimal(r.json()["liquid"]) == Decimal("1000.00")

    r = await client.get("/accounts")
    assert r.json()[0]["type"] == "checking"


async def test_patch_account(client: httpx.AsyncClient) -> None:
    await client.post("/sync")
    accounts = (await client.get("/accounts")).json()
    acct_id = accounts[0]["id"]
    r = await client.patch(
        f"/accounts/{acct_id}",
        json={"type": "savings", "promo_expires_on": "2026-12-31"},
    )
    assert r.status_code == 200
    updated = (await client.get("/accounts")).json()[0]
    assert updated["type"] == "savings"
    assert updated["promo_expires_on"] == "2026-12-31"


async def test_sync_without_adapter_is_400(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(sessionmaker, adapter=None, llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/sync")
        assert r.status_code == 400
        assert "SimpleFIN" in r.json()["detail"]


async def test_review_resolve_merchant(client: httpx.AsyncClient) -> None:
    # force a merchant review item by syncing a dirty descriptor with no LLM
    SNAP.transactions.append(
        TransactionDTO(
            id="TRN-X",
            account_id="ACT-1",
            posted_on=date(2026, 6, 20),
            amount=Decimal("-9.99"),
            description="X4529182 84756",
            raw={},
        )
    )
    try:
        await client.post("/sync")
        items = (await client.get("/review")).json()
        assert len(items) == 1 and items[0]["kind"] == "merchant"
        r = await client.post(
            f"/review/{items[0]['id']}/resolve",
            json={"resolution": {"merchant": "Mystery Gym"}},
        )
        assert r.status_code == 200
        assert (await client.get("/review")).json() == []
    finally:
        SNAP.transactions.pop()


async def test_patch_recurring_status_ends_series(client: httpx.AsyncClient) -> None:
    await client.post("/sync")
    series = (await client.get("/recurring")).json()
    series_id = series[0]["id"]
    assert Decimal((await client.get("/power")).json()["total_fixed"]) == Decimal("15.99")

    r = await client.patch(f"/recurring/{series_id}", json={"status": "ended"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    updated = (await client.get("/recurring")).json()[0]
    assert updated["status"] == "ended"
    assert Decimal((await client.get("/power")).json()["total_fixed"]) == Decimal("0")

    r = await client.patch(f"/recurring/{series_id}", json={"status": "active"})
    assert r.status_code == 200
    reactivated = (await client.get("/recurring")).json()[0]
    assert reactivated["status"] == "active"


async def test_patch_recurring_unknown_id_is_404(client: httpx.AsyncClient) -> None:
    r = await client.patch("/recurring/999999", json={"status": "ended"})
    assert r.status_code == 404


async def test_import_vesting_endpoint(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/import/vesting",
        json={"csv": "symbol,vested_quantity,unvested_quantity\nACME,40,60\n"},
    )
    assert r.status_code == 200
    assert r.json() == {"updated": 0}  # no holdings yet — still a valid parse+apply


async def test_review_resolve_recurring_cluster_validates_and_applies(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        acct = await make_account(session)
        for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
            await make_txn(
                session,
                acct,
                amount_cents=cents,
                merchant="Util Co",
                posted_on=date(2026, month, 10),
            )
        await session.commit()
        await detect_recurring(session, llm=None)

    items = (await client.get("/review")).json()
    assert len(items) == 1 and items[0]["kind"] == "recurring_cluster"
    item_id = items[0]["id"]

    r = await client.post(f"/review/{item_id}/resolve", json={"resolution": {}})
    assert r.status_code == 422

    r = await client.post(
        f"/review/{item_id}/resolve", json={"resolution": {"is_recurring": "yes"}}
    )
    assert r.status_code == 422

    r = await client.post(f"/review/{item_id}/resolve", json={"resolution": {"is_recurring": True}})
    assert r.status_code == 200

    async with sessionmaker() as session:
        stats = await detect_recurring(session, llm=None)
    assert stats.new_series == 1
