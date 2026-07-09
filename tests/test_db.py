from pathlib import Path

from sqlalchemy import text

from moneta.db import init_db, make_sessionmaker


async def test_file_backed_engine_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    engine, sm = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    async with sm() as session:
        assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await session.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
    await engine.dispose()
