from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.models import (
    AccountType,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SyncRun,
    Transaction,
    TransferLink,
)
from moneta.pipelines.transfers import effective_transfer_choices
from tests.conftest import RecordingAdapter
from tests.factories import make_account, make_price_change_item, make_series, make_txn


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(sessionmaker, [], None)),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.parametrize("via_alias", [False, True])
async def test_explicit_root_metadata_clear_never_resurrects_alias_defaults(
    client: httpx.AsyncClient, session: AsyncSession, via_alias: bool
) -> None:
    from moneta.logical import load_logical_data
    from moneta.pipelines.reconcile import rebuild_logical_derived, reconcile_provider_overlap

    root = await make_account(session, source="plaid", type=AccountType.credit)
    alias = await make_account(
        session,
        source="simplefin",
        canonical_account_id=root.id,
        financing_mode=True,
        promo_expires_on=date(2027, 1, 1),
    )
    await session.commit()
    logical = await load_logical_data(session)
    assert logical.accounts[root.id].financing_mode is True
    assert logical.accounts[root.id].promo_expires_on == date(2027, 1, 1)
    response = await client.patch(
        f"/accounts/{alias.id if via_alias else root.id}",
        json={"financing_mode": False, "promo_expires_on": None},
    )
    assert response.status_code == 200
    await session.refresh(root)
    for _ in range(2):
        await reconcile_provider_overlap(session, date(2026, 7, 15))
        await rebuild_logical_derived(session, None, date(2026, 7, 15))
        account = (await load_logical_data(session)).accounts[root.id]
        assert account.financing_mode is False
        assert account.promo_expires_on is None
        assert alias.financing_mode is True and alias.promo_expires_on == date(2027, 1, 1)


async def test_promotion_only_patch_does_not_answer_financing_question(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    from moneta.pipelines.financing import detect_financing

    root = await make_account(session, type=AccountType.credit, balance_cents=-10000)
    for month in (6, 7):
        await make_txn(session, root, posted_on=date(2026, month, 1), amount_cents=1000)
    await session.commit()
    assert (
        await client.patch(f"/accounts/{root.id}", json={"promo_expires_on": None})
    ).status_code == 200
    assert await detect_financing(session) == 1
    assert await detect_financing(session) == 0


async def test_explicit_null_promotion_conflicts_with_new_source_saved_date(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    root = await make_account(
        session, source="plaid", name="Credit", type=AccountType.credit, mask="1234"
    )
    alias = await make_account(
        session,
        source="simplefin",
        name="Credit",
        type=AccountType.credit,
        mask="1234",
        promo_expires_on=date(2027, 1, 1),
    )
    await session.commit()
    assert (
        await client.patch(f"/accounts/{root.id}", json={"promo_expires_on": None})
    ).status_code == 200
    result = await client.post(f"/accounts/{alias.id}/link", json={"canonical_account_id": root.id})
    assert result.status_code == 422
    assert "promo_expires_on" in result.json()["detail"]


async def test_logical_accounts_status_and_source_transactions(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    root = await make_account(session, source="plaid", name="Shared", balance_cents=100)
    alias = await make_account(
        session,
        source="simplefin",
        canonical_account_id=root.id,
        mask="1234",
        balance_cents=200,
        balance_date=date.today(),
    )
    survivor = await make_txn(session, root, posted_on=date.today())
    await make_txn(session, alias, posted_on=date.today(), duplicate_of_id=survivor.id)
    await session.commit()
    accounts = (await client.get("/accounts")).json()
    assert [(a["id"], a["balance_cents"]) for a in accounts] == [(root.id, 200)]
    assert (await client.get("/status")).json()["accounts"] == 1
    sources = (await client.get("/accounts?sources=true")).json()
    assert [(a["source"], a["mask"], a["canonical_account_id"]) for a in sources] == [
        ("plaid", None, None),
        ("simplefin", "1234", root.id),
    ]
    assert len((await client.get("/transactions")).json()["transactions"]) == 1
    report = (await client.get("/transactions?sources=true")).json()
    assert len(report["transactions"]) == 2
    assert report["counted_total_cents"] == 1000
    assert report["transactions"][0]["excluded_because"] == "provider_duplicate"


async def test_alias_patch_targets_root_and_records_financing_negatives(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(session, source="simplefin", canonical_account_id=root.id)
    prior = ReviewItem(
        kind=ReviewKind.financing_account,
        question="Older",
        payload={"account_id": alias.id},
        status=ReviewStatus.resolved,
        resolution={"financing": True, "resolved_by": "manual"},
    )
    pending = ReviewItem(
        kind=ReviewKind.financing_account, question="Pending", payload={"account_id": alias.id}
    )
    session.add_all([prior, pending])
    await session.commit()
    assert (
        await client.patch(
            f"/accounts/{alias.id}", json={"type": "savings", "financing_mode": False}
        )
    ).status_code == 200
    await session.refresh(root)
    await session.refresh(alias)
    await session.refresh(prior)
    await session.refresh(pending)
    assert root.type == "savings" and alias.type == "checking"
    assert prior.resolution == {"financing": True, "resolved_by": "manual"}
    assert pending.status == "resolved" and pending.resolution == {
        "financing": False,
        "resolved_by": "manual",
    }
    history = list(await session.scalars(select(ReviewItem).order_by(ReviewItem.id)))
    assert history[-1].payload["account_id"] == root.id
    assert history[-1].resolution == {"financing": False, "resolved_by": "manual"}


async def test_link_unlink_rebuilds_and_preserves_durable_decisions(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(session, source="simplefin")
    await make_txn(session, root, posted_on=date.today(), description="Coffee")
    duplicate = await make_txn(session, alias, posted_on=date.today(), description="Coffee")
    await session.commit()
    assert (
        await client.post(f"/accounts/{alias.id}/link", json={"canonical_account_id": root.id})
    ).json() == {"ok": True}
    assert len((await client.get("/accounts")).json()) == 1
    await session.refresh(duplicate)
    assert duplicate.duplicate_of_id is not None
    assert (await client.post(f"/accounts/{alias.id}/unlink")).json() == {"ok": True}
    assert len((await client.get("/accounts")).json()) == 2
    await session.refresh(duplicate)
    assert duplicate.duplicate_of_id is None
    history = list(
        await session.scalars(
            select(ReviewItem)
            .where(ReviewItem.kind == ReviewKind.account_overlap)
            .order_by(ReviewItem.id)
        )
    )
    assert [h.resolution["canonical_account_id"] for h in history if h.resolution] == [
        root.id,
        None,
    ]


@pytest.mark.parametrize("bad_target", [True, "1", 999999])
async def test_link_rejects_invalid_ids_without_mutation(
    client: httpx.AsyncClient, session: AsyncSession, bad_target: Any
) -> None:
    alias = await make_account(session, source="simplefin")
    await session.commit()
    assert (
        await client.post(f"/accounts/{alias.id}/link", json={"canonical_account_id": bad_target})
    ).status_code == 422
    await session.refresh(alias)
    assert alias.canonical_account_id is None
    assert list(await session.scalars(select(ReviewItem))) == []


@pytest.mark.parametrize("conflict", ["cycle", "currency", "financing"])
async def test_link_rejects_incompatible_graphs(
    client: httpx.AsyncClient, session: AsyncSession, conflict: str
) -> None:
    root = await make_account(session, source="plaid", financing_mode=conflict == "financing")
    alias = await make_account(
        session, source="simplefin", currency="EUR" if conflict == "currency" else "USD"
    )
    if conflict == "cycle":
        alias.canonical_account_id = root.id
    if conflict == "financing":
        session.add(
            ReviewItem(
                kind=ReviewKind.financing_account,
                question="Explicit no",
                payload={"account_id": alias.id},
                status="resolved",
                resolution={"financing": False, "resolved_by": "manual"},
            )
        )
    await session.commit()
    source, target = (root.id, alias.id) if conflict == "cycle" else (alias.id, root.id)
    response = await client.post(f"/accounts/{source}/link", json={"canonical_account_id": target})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "kind,key",
    [
        (ReviewKind.account_overlap, "canonical_account_id"),
        (ReviewKind.transaction_overlap, "duplicate_of_id"),
    ],
)
@pytest.mark.parametrize("choice", ["candidate", "none", "bool", "outsider", "missing"])
async def test_overlap_review_enforces_candidates_and_persists(
    client: httpx.AsyncClient, session: AsyncSession, kind: ReviewKind, key: str, choice: str
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(
        session,
        source="simplefin",
        canonical_account_id=root.id if kind == ReviewKind.transaction_overlap else None,
    )
    if kind == ReviewKind.account_overlap:
        source_id, target_id = alias.id, root.id
        payload_key = "account_id"
    else:
        target = await make_txn(session, root, description="A")
        source = await make_txn(session, alias, description="B")
        source_id, target_id = source.id, target.id
        payload_key = "transaction_id"
    item = ReviewItem(
        kind=kind,
        question="Same observation?",
        payload={payload_key: source_id, "candidates": [target_id]},
    )
    session.add(item)
    await session.commit()
    context = (await client.get("/review")).json()[0]["context"]
    assert context["candidates"][0]["id"] == target_id
    resolution = (
        {}
        if choice == "missing"
        else {key: {"candidate": target_id, "none": None, "bool": True, "outsider": 999999}[choice]}
    )
    response = await client.post(f"/review/{item.id}/resolve", json={"resolution": resolution})
    assert response.status_code == (200 if choice in ("candidate", "none") else 422)
    await session.refresh(item)
    assert item.status == ("resolved" if choice in ("candidate", "none") else "open")
    if choice in ("candidate", "none"):
        history = list(await session.scalars(select(ReviewItem).where(ReviewItem.kind == kind)))
        assert len(history) == 2
        assert history[-1].resolution is not None and history[-1].resolution[key] == resolution[key]


async def test_reconcile_rebuilds_estimates_without_fetching_or_sync_timestamp(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    adapter = RecordingAdapter()
    root = await make_account(session, source="plaid", mask="1234", name="Checking")
    alias = await make_account(session, source="simplefin", mask="1234", name="Checking")
    for days in (65, 35, 5):
        for account in (root, alias):
            await make_txn(
                session,
                account,
                amount_cents=-2000,
                merchant="Netflix",
                description="Netflix",
                posted_on=date.today() - timedelta(days=days),
            )
    await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(sessionmaker, [adapter], None)),
        base_url="http://test",
    ) as client:
        response = await client.post("/reconcile")
        assert response.status_code == 200
        assert response.json()["accounts_linked"] == 1
        assert response.json()["transactions_suppressed"] == 3
        assert (await client.get("/power")).json()["total_fixed_cents"] == 2000
        assert (await client.get("/status")).json()["last_sync_at"] is None
    assert adapter.since == "never-called"
    assert list(await session.scalars(select(SyncRun))) == []
    assert len(list(await session.scalars(select(Transaction)))) == 6


@pytest.mark.parametrize("violation", ["provider", "family", "currency", "amount", "sign", "date"])
async def test_transaction_review_rejects_incompatible_observations(
    client: httpx.AsyncClient, session: AsyncSession, violation: str
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(
        session,
        source="plaid" if violation == "provider" else "simplefin",
        canonical_account_id=None if violation in ("family", "currency") else root.id,
        currency="EUR" if violation == "currency" else "USD",
    )
    target = await make_txn(session, root, posted_on=date.today())
    source = await make_txn(
        session,
        alias,
        posted_on=date.today() - timedelta(days=5 if violation == "date" else 0),
        amount_cents=1000 if violation == "sign" else (-999 if violation == "amount" else -1000),
    )
    item = ReviewItem(
        kind=ReviewKind.transaction_overlap,
        question="Same?",
        payload={"transaction_id": source.id, "candidates": [target.id]},
    )
    session.add(item)
    await session.commit()
    response = await client.post(
        f"/review/{item.id}/resolve", json={"resolution": {"duplicate_of_id": target.id}}
    )
    assert response.status_code == 422
    await session.refresh(source)
    await session.refresh(item)
    assert source.duplicate_of_id is None and item.status == "open"


@pytest.mark.parametrize(
    "choice", ["bool", "outsider", "missing", "amount", "same_account", "date"]
)
async def test_transfer_resolution_rejects_invalid_choices_before_changes(
    client: httpx.AsyncClient, session: AsyncSession, choice: str
) -> None:
    root = await make_account(session)
    other = await make_account(session)
    out = await make_txn(session, root, posted_on=date.today(), amount_cents=-1000)
    inn = await make_txn(
        session,
        root if choice == "same_account" else other,
        posted_on=date.today() + timedelta(days=7 if choice == "date" else 0),
        amount_cents=999 if choice == "amount" else 1000,
    )
    item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Transfer?",
        payload={"outflow_id": out.id, "candidates": [inn.id]},
    )
    session.add(item)
    await session.commit()
    resolution = (
        {}
        if choice == "missing"
        else {
            "inflow_id": True if choice == "bool" else (999999 if choice == "outsider" else inn.id)
        }
    )
    response = await client.post(f"/review/{item.id}/resolve", json={"resolution": resolution})
    assert response.status_code == 422
    await session.refresh(item)
    assert item.status == "open"
    assert list(await session.scalars(select(TransferLink))) == []


async def test_transfer_conflict_answer_adjudicates_original_history_stably(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(session, source="simplefin", canonical_account_id=root.id)
    other = await make_account(session, source="plaid")
    out = await make_txn(session, root, posted_on=date.today(), description="Payment")
    duplicate = await make_txn(
        session, alias, posted_on=date.today(), description="Payment", duplicate_of_id=out.id
    )
    inn = await make_txn(session, other, posted_on=date.today(), amount_cents=1000)
    old = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Old",
        payload={"outflow_id": out.id, "candidates": [inn.id]},
        status="resolved",
        resolution={"inflow_id": inn.id, "resolved_by": "manual"},
    )
    no = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Other",
        payload={"outflow_id": duplicate.id, "candidates": [inn.id]},
        status="resolved",
        resolution={"inflow_id": None, "resolved_by": "manual"},
    )
    session.add_all([old, no])
    await session.flush()
    conflict = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Conflict",
        payload={
            "outflow_id": out.id,
            "candidates": [inn.id],
            "transfer_review_ids": [old.id, no.id],
            "llm_flagged": True,
        },
    )
    session.add(conflict)
    await session.commit()
    assert (
        await client.post(
            f"/review/{conflict.id}/resolve", json={"resolution": {"inflow_id": inn.id}}
        )
    ).status_code == 200
    assert (await client.post("/reconcile")).status_code == 200
    items = list(await session.scalars(select(ReviewItem)))
    for item in items:
        await session.refresh(item)
    selected, conflicts = effective_transfer_choices(
        items, {out.id: out.id, duplicate.id: out.id, inn.id: inn.id}
    )
    assert selected[out.id].inflow_id == inn.id and conflicts == {}
    assert old.resolution == {"inflow_id": inn.id, "resolved_by": "manual"}
    assert no.resolution == {"inflow_id": None, "resolved_by": "manual"}


async def test_stale_price_review_currency_is_client_error(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    foreign = await make_account(session, currency="EUR")
    await make_account(session, currency="USD")
    await make_account(session, currency="USD")
    series = await make_series(session)
    await make_txn(session, foreign, series_id=series.id, merchant="Netflix")
    item = make_price_change_item(series.id)
    session.add(item)
    await session.commit()
    response = await client.post(
        f"/review/{item.id}/resolve", json={"resolution": {"is_price_change": True}}
    )
    assert response.status_code == 422
    await session.refresh(item)
    assert item.status == "open"


async def test_transfer_alias_cannot_bypass_existing_survivor_link(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(session, source="simplefin", canonical_account_id=root.id)
    other = await make_account(session)
    out = await make_txn(session, root)
    duplicate = await make_txn(session, alias, duplicate_of_id=out.id)
    first = await make_txn(session, other, amount_cents=1000)
    second = await make_txn(session, other, amount_cents=1000)
    session.add(
        TransferLink(outflow_id=out.id, inflow_id=first.id, confidence=1.0, method="manual")
    )
    item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Stale alias question",
        payload={"outflow_id": duplicate.id, "candidates": [second.id]},
    )
    session.add(item)
    await session.commit()
    response = await client.post(
        f"/review/{item.id}/resolve", json={"resolution": {"inflow_id": second.id}}
    )
    assert response.status_code == 409
    links = list(await session.scalars(select(TransferLink)))
    assert [(link.outflow_id, link.inflow_id) for link in links] == [(out.id, first.id)]
