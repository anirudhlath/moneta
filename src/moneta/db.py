from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from moneta.models import Base


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


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
