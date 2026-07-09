from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from moneta.db import init_db, make_sessionmaker
from moneta.models import Base


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
        await conn.run_sync(Base.metadata.create_all)  # simulates a pre-Alembic DB
    await init_db(engine)  # must stamp, then upgrade cleanly (no "table exists" error)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "alembic_version" in tables
    await engine.dispose()


async def test_init_db_is_idempotent() -> None:
    engine, _ = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    await init_db(engine)
    await engine.dispose()
