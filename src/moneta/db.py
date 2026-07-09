from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_BASELINE = "0001"


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
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    insp = inspect(conn)
    if insp.has_table("accounts") and not insp.has_table("alembic_version"):
        command.stamp(cfg, _BASELINE)  # adopt a pre-Alembic database at the baseline
    command.upgrade(cfg, "head")


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
