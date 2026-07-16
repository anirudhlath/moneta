import os
import time
from collections.abc import AsyncIterator, Iterator
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


@pytest.fixture(autouse=True)
def _utc_timezone() -> Iterator[None]:
    """_ts_to_date converts in local time; pin TZ so tests don't depend on the machine."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


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
        self.since: date | None = None

    async def fetch(self, since: date | None = None) -> Snapshot:
        self.since = since
        return self.snapshot


class RecordingAdapter:
    """Records the `since` value run_sync passes to fetch."""

    def __init__(self) -> None:
        self.since: date | None | str = "never-called"

    async def fetch(self, since: date | None = None) -> Snapshot:
        self.since = since
        return Snapshot(accounts=[], transactions=[], holdings=[])
