import os
from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.aggregator.base import Snapshot
from moneta.db import init_db, make_sessionmaker


@pytest.fixture(autouse=True)
def _clean_moneta_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must be hermetic: the developer's shell may export MONETA_* vars."""
    for key in [k for k in os.environ if k.startswith("MONETA_")]:
        monkeypatch.delenv(key)


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine, sm = make_sessionmaker("sqlite+aiosqlite://")
    await init_db(engine)
    yield sm
    await engine.dispose()


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


class FakeAdapter:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    async def fetch(self, since: date | None = None) -> Snapshot:
        return self.snapshot
