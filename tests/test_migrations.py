from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import moneta.db
from moneta.db import init_db, make_sessionmaker
from moneta.models import Account, Base
from tests.factories import make_account, make_series, make_txn


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(moneta.db.__file__).parent / "migrations"))
    return ScriptDirectory.from_config(cfg)


def _create_pre_alembic_db(conn: Connection) -> None:
    """Replay the frozen 0001 baseline: exactly what a real pre-Alembic DB contains."""
    with Operations.context(MigrationContext.configure(conn)):
        _script_directory().get_revision("0001").module.upgrade()


def _schema(conn: Connection) -> dict[str, list[tuple[str, str, bool]]]:
    insp = inspect(conn)
    return {
        t: [(c["name"], str(c["type"]), bool(c["nullable"])) for c in insp.get_columns(t)]
        for t in insp.get_table_names()
        if t != "alembic_version"
    }


async def _schema_of(engine: AsyncEngine) -> dict[str, list[tuple[str, str, bool]]]:
    async with engine.connect() as conn:
        return await conn.run_sync(_schema)


def _alembic_config(conn: Connection) -> Config:
    """A config that runs the real migrations on `conn` (env.py requires the connection)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(moneta.db.__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    return cfg


def _upgrade_to(conn: Connection, revision: str) -> None:
    """Stop at `revision`, unlike init_db, which always goes to head."""
    command.upgrade(_alembic_config(conn), revision)


# an account row with only the columns every revision since 0001 requires
_INSERT_ACCOUNT = (
    "INSERT INTO accounts (aggregator_id, name, org_name, type, currency, balance_cents,"
    " balance_date) VALUES ('ACT-1', 'Everyday Checking', 'Acme Bank', 'checking', 'USD',"
    " 10000, '2026-07-01')"
)


def _downgrade_to(conn: Connection, revision: str) -> None:
    command.downgrade(_alembic_config(conn), revision)


def _autoincrement_tables(conn: Connection) -> set[str]:
    rows = conn.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'table'").all()
    return {name for name, sql in rows if "AUTOINCREMENT" in (sql or "")}


def _constraints(conn: Connection) -> tuple[object, ...]:
    """What a table rebuild must keep: constraints, indexes, column order and defaults."""
    insp = inspect(conn)
    return (
        insp.get_unique_constraints("transactions"),
        sorted(insp.get_foreign_keys("transactions"), key=lambda fk: fk["constrained_columns"]),
        insp.get_indexes("transactions"),
        [(c["name"], c["default"]) for c in insp.get_columns("transactions")],
    )


async def test_migrations_match_create_all_schema() -> None:
    via_models, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with via_models.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    via_migrations, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(via_migrations)
    assert await _schema_of(via_models) == await _schema_of(via_migrations)
    await via_models.dispose()
    await via_migrations.dispose()


async def test_init_db_adopts_pre_migration_database() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(_create_pre_alembic_db)
    await init_db(engine)  # must stamp 0001, then upgrade through 0002+
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "alembic_version" in tables
    assert "sync_runs" in tables
    await engine.dispose()


async def test_init_db_is_idempotent() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    await init_db(engine)
    await engine.dispose()


def test_head_constant_matches_script_directory() -> None:
    assert _script_directory().get_current_head() == moneta.db._HEAD


async def test_0007_allows_several_series_per_merchant() -> None:
    engine, sm = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    async with engine.connect() as conn:
        uniques = await conn.run_sync(
            lambda c: inspect(c).get_unique_constraints("recurring_series")
        )
    assert uniques == []
    async with sm() as session:
        await make_series(session, merchant="Acme Fitness", expected_cents=-999)
        await make_series(session, merchant="Acme Fitness", expected_cents=-4999)
        await session.commit()
    await engine.dispose()


async def test_0008_gives_existing_accounts_an_empty_raw() -> None:
    engine, sm = make_sessionmaker("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_to, "0007")
        await conn.exec_driver_sql(_INSERT_ACCOUNT)
    await init_db(engine)  # 0007 -> 0008
    async with sm() as session:
        account = (await session.scalars(select(Account))).one()
        assert account.raw == {}
    await engine.dispose()


async def test_transactions_autoincrement_matches_the_models() -> None:
    via_models, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with via_models.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    via_migrations, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(via_migrations)
    for engine in (via_models, via_migrations):
        async with engine.connect() as conn:
            assert await conn.run_sync(_autoincrement_tables) == {"transactions"}
        await engine.dispose()


async def test_a_deleted_transaction_id_is_never_reused(session: AsyncSession) -> None:
    account = await make_account(session)
    await make_txn(session, account)
    newest = await make_txn(session, account)
    deleted_id = newest.id
    await session.delete(newest)
    await session.flush()
    again = await make_txn(session, account)
    assert again.id > deleted_id  # without AUTOINCREMENT SQLite hands out deleted_id again


async def test_0009_keeps_rows_ids_and_constraints() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_to, "0007")
        await conn.exec_driver_sql(_INSERT_ACCOUNT)
        for txn_id, duplicate_of in ((3, None), (7, 3), (12, None)):
            await conn.exec_driver_sql(
                "INSERT INTO transactions (id, account_id, aggregator_id, posted_on,"
                " amount_cents, description, raw, duplicate_of_id)"
                " VALUES (?, 1, ?, '2026-07-01', -12000, 'CORNER CURRY ELMTON', '{}', ?)",
                (txn_id, f"TRN-{txn_id}", duplicate_of),
            )
        before = await conn.run_sync(_constraints)
    await init_db(engine)  # 0007 -> 0008 -> 0009
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT id, aggregator_id, duplicate_of_id FROM transactions ORDER BY id"
        )
        assert [tuple(r) for r in rows] == [
            (3, "TRN-3", None),
            (7, "TRN-7", 3),
            (12, "TRN-12", None),
        ]
        assert await conn.run_sync(_constraints) == before
        seq = await conn.exec_driver_sql(
            "SELECT seq FROM sqlite_sequence WHERE name = 'transactions'"
        )
        assert seq.scalar_one() == 12  # new ids land above every id the table holds
    await engine.dispose()


async def test_0009_downgrade_restores_the_plain_table() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_INSERT_ACCOUNT)
        await conn.exec_driver_sql(
            "INSERT INTO transactions (account_id, aggregator_id, posted_on, amount_cents,"
            " description, raw)"
            " VALUES (1, 'TRN-1', '2026-07-01', -12000, 'CORNER CURRY ELMTON', '{}')"
        )
        await conn.run_sync(_downgrade_to, "0008")
        assert await conn.run_sync(_autoincrement_tables) == set()
        ids = await conn.exec_driver_sql("SELECT id FROM transactions")
        assert list(ids.scalars()) == [1]
    await engine.dispose()


class _DiskFull(RuntimeError):
    """Stands in for any failure partway through the 0009 table copy."""


def _fail_on_drop_transactions(
    _conn: Connection, _cursor: Any, statement: str, *_rest: Any
) -> None:
    if statement.strip().startswith("DROP TABLE transactions"):
        raise _DiskFull("database or disk is full")


def _upgrade_state(conn: Connection) -> tuple[object, list[str], bool, list[tuple[Any, ...]]]:
    """Everything a half-applied upgrade could leave behind."""
    insp = inspect(conn)
    version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    leftovers = [t for t in insp.get_table_names() if t.startswith("_alembic_tmp")]
    has_raw = "raw" in {c["name"] for c in insp.get_columns("accounts")}
    rows = conn.exec_driver_sql(
        "SELECT id, aggregator_id, duplicate_of_id FROM transactions ORDER BY id"
    ).all()
    return version, leftovers, has_raw, [tuple(r) for r in rows]


@pytest.mark.parametrize("start", ["0007", "0008"])
async def test_a_failed_upgrade_changes_nothing_and_the_rerun_is_clean(
    tmp_path: Path, start: str
) -> None:
    # a file database: the live one is, and so is its commit behaviour
    engine, _ = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_to, start)
        await conn.exec_driver_sql(_INSERT_ACCOUNT)
        for txn_id, duplicate_of in ((3, None), (7, 3), (12, None)):
            await conn.exec_driver_sql(
                "INSERT INTO transactions (id, account_id, aggregator_id, posted_on,"
                " amount_cents, description, raw, duplicate_of_id)"
                " VALUES (?, 1, ?, '2026-07-01', -12000, 'CORNER CURRY ELMTON', '{}', ?)",
                (txn_id, f"TRN-{txn_id}", duplicate_of),
            )
    rows = [(3, "TRN-3", None), (7, "TRN-7", 3), (12, "TRN-12", None)]

    event.listen(engine.sync_engine, "before_cursor_execute", _fail_on_drop_transactions)
    try:
        with pytest.raises(_DiskFull):
            await init_db(engine)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _fail_on_drop_transactions)

    async with engine.connect() as conn:
        # 0008's ADD COLUMN must not outlive the failed run, or the rerun hits
        # "duplicate column name"; the half-built copy must not either
        assert await conn.run_sync(_upgrade_state) == (start, [], start != "0007", rows)

    await init_db(engine)
    async with engine.connect() as conn:
        assert await conn.run_sync(_upgrade_state) == ("0009", [], True, rows)
        assert await conn.run_sync(_autoincrement_tables) == {"transactions"}
    await engine.dispose()
