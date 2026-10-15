from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

import moneta.db
from moneta.db import init_db, make_sessionmaker
from moneta.models import Account, Base
from tests.factories import make_series


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
