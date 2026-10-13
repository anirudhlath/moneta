"""Rehearse rollout on a frozen, synthetic 0005 database stored on disk."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

import moneta.db
from moneta.aggregator.base import AccountDTO, Snapshot
from moneta.api import create_app
from moneta.db import init_db, make_sessionmaker
from moneta.logical import load_logical_data
from moneta.models import AccountType
from moneta.pipelines.ingest import ingest_snapshot
from moneta.pipelines.reconcile import rebuild_logical_derived, reconcile_provider_overlap
from moneta.views.networth import net_worth_report

TODAY = date(2026, 7, 15)


def _seed_0005(conn: Connection) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(moneta.db.__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, "0005")
    assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0005"
    assert "mask" not in {c["name"] for c in inspect(conn).get_columns("accounts")}
    assert "duplicate_of_id" not in {c["name"] for c in inspect(conn).get_columns("transactions")}
    conn.exec_driver_sql(
        "INSERT INTO accounts "
        "(id, aggregator_id, name, org_name, type, currency, balance_cents, balance_date, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "sf-broker",
                "Brokerage (1234)",
                "Example Bank",
                "brokerage",
                "USD",
                80000,
                "2026-07-14",
                "simplefin",
            ),
            (
                2,
                "plaid-broker",
                "Brokerage",
                "Example Bank",
                "brokerage",
                "USD",
                90000,
                "2026-07-15",
                "plaid",
            ),
        ],
    )
    conn.exec_driver_sql(
        "INSERT INTO transactions "
        "(id, account_id, aggregator_id, posted_on, amount_cents, description, merchant, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                1,
                "sf-dividend",
                "2026-07-01",
                2300,
                "Example Dividend",
                "Example Dividend",
                '{"source":"simplefin","memo":"dividend","amount":"23.00"}',
            ),
            (
                2,
                2,
                "plaid-dividend",
                "2026-07-01",
                2300,
                "Example Dividend",
                "Example Dividend",
                '{"source":"plaid","memo":"dividend","amount":"-23.00"}',
            ),
            (
                3,
                2,
                "plaid-archive",
                "2024-10-14",
                -1700,
                "Archive Purchase",
                "Archive Purchase",
                '{"source":"plaid","memo":"older unique","amount":"17.00"}',
            ),
            (
                4,
                1,
                "sf-new",
                "2026-07-14",
                -3100,
                "New Purchase",
                "New Purchase",
                '{"source":"simplefin","memo":"new unique","amount":"-31.00"}',
            ),
            (
                5,
                2,
                "plaid-only",
                "2026-06-02",
                -1100,
                "Other Purchase",
                "Other Purchase",
                '{"source":"plaid","memo":"unique","amount":"11.00"}',
            ),
        ],
    )
    conn.exec_driver_sql(
        "INSERT INTO holdings "
        "(id, account_id, symbol, quantity, market_value_cents, "
        "vested_quantity, unvested_quantity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(1, 1, "EXAMPLE", 10.0, 80000, 4.0, 6.0), (2, 2, "EXAMPLE", 10.0, 90000, None, None)],
    )
    conn.exec_driver_sql(
        "INSERT INTO review_items (id, kind, question, payload, status, resolution) "
        "VALUES (?, ?, ?, ?, 'resolved', ?)",
        [
            (
                1,
                "merchant",
                "Dividend name?",
                '{"raw_descriptor":"Example Dividend"}',
                '{"merchant":"Example Dividend","resolved_by":"manual"}',
            ),
            (
                2,
                "financing_account",
                "Financing?",
                '{"account_id":1}',
                '{"financing":false,"resolved_by":"manual"}',
            ),
        ],
    )
    conn.exec_driver_sql(
        "INSERT INTO sync_runs (id, started_at, finished_at, success, report) "
        "VALUES (1, '2026-07-14 06:00:00', '2026-07-14 06:01:00', 1, '{}')"
    )


async def _fingerprint(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    """Read financial facts independently of the new projection/relationship columns."""
    columns = {
        "accounts": (
            "id, aggregator_id, source, name, org_name, type, currency, balance_cents, "
            "balance_date, promo_expires_on, financing_mode"
        ),
        "transactions": "id, account_id, aggregator_id, posted_on, amount_cents, description, raw",
        "holdings": (
            "id, account_id, symbol, quantity, market_value_cents, "
            "vested_quantity, unvested_quantity"
        ),
        "review_items": "id, kind, question, payload, status, resolution, created_on",
        "sync_runs": "id, started_at, finished_at, success, error, report",
    }
    async with engine.connect() as conn:
        return {
            table: [
                tuple(row)
                for row in await conn.exec_driver_sql(
                    f"SELECT {fields} FROM {table}"
                    + (" WHERE id IN (1, 2)" if table == "review_items" else "")
                    + " ORDER BY id"
                )
            ]
            for table, fields in columns.items()
        }


async def test_reconcile_file_database_preserves_raw_money(tmp_path: Path) -> None:
    # Losing observations during migration, summing aliases, or dropping old history
    # must fail this rehearsal. Its expected account/transaction IDs are hand-derived.
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'old-moneta.db'}"
    engine, _ = make_sessionmaker(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_seed_0005)
        before = await _fingerprint(engine)
    finally:
        await engine.dispose()

    engine, sessions = make_sessionmaker(db_url)
    try:
        await init_db(engine)
        assert await _fingerprint(engine) == before
        async with sessions() as session:
            # Existing 0005 accounts have no suffix column. Enrich identity using
            # the normal ingestion boundary, with unchanged synthetic observations.
            await ingest_snapshot(
                session,
                Snapshot(
                    accounts=[
                        AccountDTO(
                            id=provider_id,
                            name=name,
                            org_name="Example Bank",
                            currency="USD",
                            balance=balance,
                            balance_date=balance_date,
                            type_hint=AccountType.brokerage,
                            source=source,
                            mask="1234",
                        )
                        for provider_id, name, balance, balance_date, source in [
                            (
                                "sf-broker",
                                "Brokerage (1234)",
                                Decimal("800.00"),
                                date(2026, 7, 14),
                                "simplefin",
                            ),
                            ("plaid-broker", "Brokerage", Decimal("900.00"), TODAY, "plaid"),
                        ]
                    ],
                    transactions=[],
                    holdings=[],
                ),
            )
            await session.commit()
        assert await _fingerprint(engine) == before
        reports: list[dict[str, Any]] = []
        for iteration in range(2):
            await init_db(engine)
            async with sessions() as session:
                stats = await reconcile_provider_overlap(session, TODAY)
                await rebuild_logical_derived(session, None, TODAY)
                assert stats.accounts_linked == (1 if iteration == 0 else 0)
                assert stats.transactions_suppressed == (1 if iteration == 0 else 0)
                assert stats.transactions_restored == 0
                if iteration:
                    assert stats.reviews_opened == 0
                    assert stats.reviews_superseded == 0
                logical = await load_logical_data(session)
                assert [
                    (a.id, a.balance_cents, a.balance_date) for a in logical.accounts.values()
                ] == [(1, 90000, TODAY)]
                assert [t.id for t in logical.transactions] == [1, 3, 4, 5]
                assert min(t.posted_on for t in logical.transactions) == date(2024, 10, 14)
                assert [
                    (
                        h.account_id,
                        h.quantity,
                        h.market_value_cents,
                        h.vested_quantity,
                        h.unvested_quantity,
                    )
                    for h in logical.holdings
                ] == [(1, 10.0, 90000, 4.0, 6.0)]
                networth = (await net_worth_report(session)).model_dump(mode="json")
                assert (
                    networth["vested_holdings_cents"],
                    networth["unvested_potential_cents"],
                    networth["net_worth_cents"],
                ) == (36000, 54000, 36000)
            assert await _fingerprint(engine) == before
            async with engine.connect() as conn:
                bookkeeping = {
                    table: [tuple(row) for row in await conn.exec_driver_sql(query)]
                    for table, query in {
                        "accounts": "SELECT id, canonical_account_id FROM accounts ORDER BY id",
                        "transactions": "SELECT id, duplicate_of_id FROM transactions ORDER BY id",
                        "reviews": "SELECT * FROM review_items ORDER BY id",
                    }.items()
                }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(sessions, [], None)),
                base_url="http://test",
            ) as client:
                accounts = (await client.get("/accounts")).json()
                assert [(a["id"], a["balance_cents"]) for a in accounts] == [(1, 90000)]
                assert (await client.get("/status")).json()["accounts"] == 1
                sources = (await client.get("/accounts?sources=true")).json()
                assert [(a["id"], a["source"], a["canonical_account_id"]) for a in sources] == [
                    (1, "simplefin", None),
                    (2, "plaid", 1),
                ]
                params: dict[str, str | int] = {
                    "start": "2024-10-14",
                    "end": "2026-07-15",
                    "account_id": 1,
                }
                history = (await client.get("/transactions", params=params)).json()
                assert {t["id"] for t in history["transactions"]} == {1, 3, 4, 5}
                audit = (
                    await client.get("/transactions", params={**params, "sources": "true"})
                ).json()
                assert len(audit["transactions"]) == 5
                assert [
                    t["id"]
                    for t in audit["transactions"]
                    if t["excluded_because"] == "provider_duplicate"
                ] == [2]
                reviews = (await client.get("/review")).json()
                reports.append(
                    {
                        "accounts": accounts,
                        "sources": sources,
                        "history": history,
                        "audit": audit,
                        "reviews": reviews,
                        "networth": networth,
                        "bookkeeping": bookkeeping,
                    }
                )
        assert reports[1] == reports[0]
    finally:
        await engine.dispose()
