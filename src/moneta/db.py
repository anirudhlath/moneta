from pathlib import Path
from typing import Any

from sqlalchemy import Connection, event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_BASELINE = "0001"
# newest revision in migrations/versions/ — bump alongside each new migration;
# tests/test_migrations.py pins this against the script directory
_HEAD = "0004"


def make_sessionmaker(db_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    kwargs: dict[str, object] = {}
    in_memory = db_url.endswith("aiosqlite://")
    if in_memory:  # in-memory: share one connection across sessions
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(db_url, **kwargs)
    if not in_memory:  # serve + in-process CLI may write the same file concurrently

        @event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _upgrade(conn: Connection) -> None:
    insp = inspect(conn)
    if insp.has_table("alembic_version"):
        current = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        if current == _HEAD:
            return  # common case (every CLI command) — skip the alembic machinery

    from alembic import command  # deferred: only imported when migrations actually run
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    if insp.has_table("accounts") and not insp.has_table("alembic_version"):
        command.stamp(cfg, _BASELINE)  # adopt a pre-Alembic database at the baseline
    command.upgrade(cfg, "head")


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
