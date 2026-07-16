from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.aggregator.base import AccountDTO, MergedAdapter, Snapshot, TransactionDTO
from moneta.aggregator.plaid import PlaidAdapter, PlaidItem, items_path, save_items
from moneta.aggregator.simplefin import SimpleFINAdapter
from moneta.api import _build_adapter, create_app
from moneta.config import Settings
from moneta.db import init_db, make_sessionmaker
from moneta.models import EventKind, RecurringSeries, ReviewItem, SeriesEvent
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.run import RESYNC_OVERLAP_DAYS
from tests.conftest import FakeAdapter, RecordingAdapter
from tests.factories import (
    make_account,
    make_price_change_item,
    make_series,
    make_series_event,
    make_txn,
)

# The /sync and /power endpoints resolve date.today() at request time, so snapshot
# dates must be relative — pinned dates would go stale as real time passes.
_TODAY = date.today()

SNAP = Snapshot(
    accounts=[
        AccountDTO(
            id="ACT-1",
            name="Premier Checking",
            org_name="Chase",
            currency="USD",
            balance=Decimal("1000.00"),
            balance_date=_TODAY,
        ),
    ],
    transactions=[
        TransactionDTO(
            id=f"TRN-{i}",
            account_id="ACT-1",
            posted_on=_TODAY - timedelta(days=days_ago),
            amount=Decimal("-15.99"),
            description="NETFLIX.COM",
            raw={},
        )
        for i, days_ago in enumerate((75, 45, 15))
    ],
    holdings=[],
)


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(create_app(sessionmaker, adapter=FakeAdapter(SNAP), llm=None)) as c:
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


async def test_sync_full_param_forces_epoch_pull(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, posted_on=date(2026, 7, 5))
    await session.commit()
    adapter = RecordingAdapter()
    async with _client(create_app(sessionmaker, adapter=adapter, llm=None)) as c:
        assert (await c.post("/sync")).status_code == 200
        assert adapter.since == date(2026, 7, 5) - timedelta(days=RESYNC_OVERLAP_DAYS)
        assert (await c.post("/sync", params={"full": "true"})).status_code == 200
        assert adapter.since == date(1970, 1, 1)


async def test_sync_without_adapter_is_400(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with _client(create_app(sessionmaker, adapter=None, llm=None)) as c:
        r = await c.post("/sync")
        assert r.status_code == 400
        assert "setup simplefin" in r.json()["detail"]
        assert "setup plaid" in r.json()["detail"]


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


async def test_events_include_series_merchant(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        series = await make_series(session)
        await make_series_event(session, series)
        await session.commit()
    r = await client.get("/recurring/events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 1
    assert events[0]["merchant"] == "Netflix"


async def test_events_with_dangling_series_still_listed(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """SQLite doesn't enforce FKs here — an orphaned event must not vanish silently."""
    async with sessionmaker() as session:
        session.add(
            SeriesEvent(
                series_id=999,
                kind=EventKind.missed,
                occurred_on=date(2026, 7, 1),
                details={},
            )
        )
        await session.commit()
    events = (await client.get("/recurring/events")).json()
    assert len(events) == 1
    assert events[0]["merchant"] == "series 999"


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
        await detect_recurring(session, llm=None, today=date(2026, 7, 1))

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
        stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1


async def test_review_context_enrichment(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from moneta.models import AccountType

    async with sessionmaker() as session:
        checking = await make_account(session, type=AccountType.checking)
        savings = await make_account(session, type=AccountType.savings, name="My Savings")
        out = await make_txn(
            session,
            checking,
            amount_cents=-50000,
            posted_on=date(2026, 7, 1),
            description="ACH TRANSFER",
        )
        c1 = await make_txn(
            session,
            savings,
            amount_cents=50000,
            posted_on=date(2026, 7, 2),
            description="DEPOSIT A",
        )
        c2 = await make_txn(
            session,
            savings,
            amount_cents=50000,
            posted_on=date(2026, 7, 3),
            description="DEPOSIT B",
        )
        for month, cents in ((4, -2000), (5, -9000), (6, -4500)):
            await make_txn(
                session,
                checking,
                amount_cents=cents,
                merchant="Util Co",
                posted_on=date(2026, month, 10),
            )
        session.add(
            ReviewItem(
                kind="transfer_pair",
                question="which?",
                payload={"outflow_id": out.id, "candidates": [c1.id, c2.id]},
            )
        )
        session.add(
            ReviewItem(
                kind="recurring_cluster",
                question="recurring?",
                payload={"merchant": "Util Co", "direction": "outflow"},
            )
        )
        await session.commit()

    async with _client(create_app(sessionmaker, adapter=None, llm=None)) as client:
        items = (await client.get("/review")).json()

    tp = next(i for i in items if i["kind"] == "transfer_pair")
    assert tp["context"]["outflow"]["amount_cents"] == -50000
    assert tp["context"]["outflow"]["description"] == "ACH TRANSFER"
    cands = tp["context"]["candidates"]
    assert [c["description"] for c in cands] == ["DEPOSIT A", "DEPOSIT B"]
    assert cands[0]["account"] == "My Savings"
    assert cands[0]["amount_cents"] == 50000
    assert cands[0]["id"] == c1.id

    rc = next(i for i in items if i["kind"] == "recurring_cluster")
    samples = rc["context"]["samples"]
    assert len(samples) == 3
    assert samples[0]["amount_cents"] == -4500  # newest first, sign intact
    assert rc["context"]["direction"] == "outflow"


async def test_review_resolve_price_change_validates_and_applies(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        series = await make_series(session)
        series_id = series.id
        session.add(make_price_change_item(series_id))
        await session.commit()

    items = (await client.get("/review")).json()
    assert len(items) == 1 and items[0]["kind"] == "price_change"
    item_id = items[0]["id"]
    assert items[0]["context"]["old_amount_cents"] == -1599
    assert items[0]["context"]["new_amount_cents"] == -1899

    r = await client.post(f"/review/{item_id}/resolve", json={"resolution": {}})
    assert r.status_code == 422

    r = await client.post(
        f"/review/{item_id}/resolve", json={"resolution": {"is_price_change": True}}
    )
    assert r.status_code == 200
    async with sessionmaker() as session:
        refreshed = (
            await session.execute(select(RecurringSeries).where(RecurringSeries.id == series_id))
        ).scalar_one()
        assert refreshed.expected_cents == -1899


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(config_dir=tmp_path, db_path=tmp_path / "m.db", **kwargs)  # type: ignore[arg-type]


def test_build_adapter_none_when_nothing_configured(tmp_path: Path) -> None:
    # conftest's autouse _clean_moneta_env fixture already strips MONETA_* env vars
    assert _build_adapter(_settings(tmp_path)) is None


def test_build_adapter_simplefin_only(tmp_path: Path) -> None:
    adapter = _build_adapter(
        _settings(tmp_path, simplefin_access_url="https://u:p@bridge.example/simplefin")
    )
    assert isinstance(adapter, SimpleFINAdapter)


def test_build_adapter_plaid_requires_items(tmp_path: Path) -> None:
    s = _settings(tmp_path, plaid_client_id="cid", plaid_secret="sec", plaid_env="sandbox")
    assert _build_adapter(s) is None  # creds but no linked items
    save_items(items_path(tmp_path), [PlaidItem(item_id="it-1", access_token="a")])
    assert isinstance(_build_adapter(s), PlaidAdapter)


def test_build_adapter_merges_simplefin_and_plaid(tmp_path: Path) -> None:
    save_items(items_path(tmp_path), [PlaidItem(item_id="it-1", access_token="a")])
    adapter = _build_adapter(
        _settings(
            tmp_path,
            simplefin_access_url="https://u:p@bridge.example/simplefin",
            plaid_client_id="cid",
            plaid_secret="sec",
        )
    )
    assert isinstance(adapter, MergedAdapter)


def test_build_adapter_tolerates_corrupt_items_file(tmp_path: Path) -> None:
    items_path(tmp_path).write_text("{not json")
    s = _settings(tmp_path, plaid_client_id="cid", plaid_secret="sec")
    assert _build_adapter(s) is None  # no crash: sync just runs without Plaid


async def test_sync_last_endpoint(client: httpx.AsyncClient) -> None:
    assert (await client.get("/sync/last")).json() is None
    await client.post("/sync")
    body = (await client.get("/sync/last")).json()
    assert body["status"] == "ok"
    assert body["success"] is True
    assert body["report"]["ingest"]["new_transactions"] == 3


async def test_bearer_token_enforced_when_configured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with _client(create_app(sessionmaker, adapter=None, llm=None, api_token="s3cret")) as c:
        assert (await c.get("/accounts")).status_code == 401
        assert (
            await c.get("/accounts", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await c.get("/accounts", headers={"Authorization": "Bearer s3cret"})
        ).status_code == 200
        # app-level dependencies don't cover the docs routes — they must be disabled
        assert (await c.get("/openapi.json")).status_code == 404
        assert (await c.get("/docs")).status_code == 404


async def test_backup_vacuum_into(tmp_path: Path) -> None:
    engine, sm = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    dest = tmp_path / "out.db"
    async with _client(create_app(sm, adapter=None, llm=None, engine=engine)) as c:
        r = await c.post("/backup", json={"dest": str(dest)})
        assert r.status_code == 200
        assert r.json() == {"path": str(dest)}
        assert dest.exists() and dest.stat().st_size > 0
        assert dest.stat().st_mode & 0o777 == 0o600
        assert (await c.post("/backup", json={"dest": str(dest)})).status_code == 409
    await engine.dispose()


async def test_backup_requires_file_backed_db(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # no engine → nothing to back up
    async with _client(create_app(sessionmaker, adapter=None, llm=None)) as c:
        assert (await c.post("/backup", json={})).status_code == 400


async def test_patch_unknown_account_is_404(client: httpx.AsyncClient) -> None:
    r = await client.patch("/accounts/999999", json={"type": "savings"})
    assert r.status_code == 404


async def test_resolve_unknown_review_item_is_404(client: httpx.AsyncClient) -> None:
    r = await client.post("/review/999999/resolve", json={"resolution": {}})
    assert r.status_code == 404


async def test_import_vesting_malformed_csv_is_422(client: httpx.AsyncClient) -> None:
    r = await client.post("/import/vesting", json={"csv": "ticker,vested\nACME,40\n"})
    assert r.status_code == 422


async def test_reactivating_stale_series_bumps_next_expected_forward(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    from moneta.models import SeriesStatus
    from tests.factories import make_series

    s = await make_series(session, status=SeriesStatus.ended, next_expected_on=date(2026, 1, 15))
    await session.commit()
    series_id = s.id

    async with _client(create_app(sessionmaker, adapter=None, llm=None)) as c:
        assert (
            await c.patch(f"/recurring/{series_id}", json={"status": "active"})
        ).status_code == 200
        row = next(r for r in (await c.get("/recurring")).json() if r["id"] == series_id)
    assert date.fromisoformat(row["next_expected_on"]) >= date.today()
