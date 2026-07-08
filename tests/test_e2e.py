from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO
from moneta.api import create_app
from tests.conftest import FakeAdapter

# The API endpoints resolve date.today() at request time, so all snapshot
# dates must be computed relative to today or the scenario silently ages out
# (paychecks fall outside the detection window, current-month spend goes to 0).
TODAY = date.today()


def _month_start(months_back: int) -> date:
    """First of the month `months_back` months before the current month."""
    y, m = TODAY.year, TODAY.month - months_back
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _acct(id_: str, name: str, balance: str) -> AccountDTO:
    return AccountDTO(
        id=id_,
        name=name,
        org_name="Bank",
        currency="USD",
        balance=Decimal(balance),
        balance_date=TODAY,
    )


def _txn(id_: str, acct: str, on: date, amount: str, desc: str) -> TransactionDTO:
    return TransactionDTO(
        id=id_,
        account_id=acct,
        posted_on=on,
        amount=Decimal(amount),
        description=desc,
        raw={},
    )


SNAPSHOT = Snapshot(
    accounts=[
        _acct("CHK", "Premier Checking", "4200.00"),
        _acct("SAV", "Online Savings", "10000.00"),
        _acct("CC", "Freedom Credit Card", "-350.00"),
        _acct("SYN", "Synchrony Financing Loan", "-1215.00"),
    ],
    transactions=[
        # biweekly paychecks, most recent today
        *[
            _txn(f"PAY{i}", "CHK", TODAY - timedelta(days=14 * i), "2500.00", "ACME CORP PAYROLL")
            for i in range(5)
        ],
        # monthly rent: current month and two prior. Anchoring recurring
        # charges to the 1st keeps the newest occurrence <= today on every
        # run date (even when today IS the 1st), so the series always forms
        # and its current-month txn is series-linked, not discretionary.
        *[
            _txn(f"RENT{n}", "CHK", _month_start(n), "-1800.00", "OAKWOOD PROPERTIES RENT")
            for n in (2, 1, 0)
        ],
        # netflix on the credit card: three monthly charges ending this month
        # (recurring detection needs >= 3 occurrences to form a series)
        *[_txn(f"NFLX{n}", "CC", _month_start(n), "-15.99", "NETFLIX.COM") for n in (2, 1, 0)],
        # cc payments checking -> cc, same-day pairs in the two prior months
        *[
            t
            for n in (2, 1)
            for t in (
                _txn(
                    f"CCP{n}",
                    "CHK",
                    _month_start(n) + timedelta(days=19),
                    "-215.99",
                    "CHASE CARD AUTOPAY PAYMENT",
                ),
                _txn(
                    f"CCR{n}",
                    "CC",
                    _month_start(n) + timedelta(days=19),
                    "215.99",
                    "AUTOPAY PAYMENT THANK YOU",
                ),
            )
        ],
        # synchrony payments checking -> loan, monthly ending this month
        *[
            t
            for n in (2, 1, 0)
            for t in (
                _txn(f"SYP{n}", "CHK", _month_start(n), "-135.00", "SYNCHRONY BANK PAYMENT"),
                _txn(f"SYR{n}", "SYN", _month_start(n), "135.00", "SYNCHRONY BANK PAYMENT"),
            )
        ],
        # the only discretionary spend this month
        _txn("DIN1", "CC", TODAY, "-62.40", "GOOD RESTAURANT"),
    ],
    holdings=[],
)


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(sessionmaker, adapter=FakeAdapter(SNAPSHOT), llm=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_full_pipeline_power_and_obligations(client: httpx.AsyncClient) -> None:
    assert (await client.post("/sync")).status_code == 200

    power = (await client.get("/power")).json()
    # income: 2500 biweekly -> 5416.67/mo
    assert Decimal(power["monthly_income"]) == Decimal("5416.67")
    fixed_merchants = {line["merchant"] for line in power["fixed_costs"]}
    # rent + netflix + synchrony are fixed; CC payment series must NOT be
    assert any("Rent" in m or "Oakwood" in m for m in fixed_merchants)
    assert any("Netflix" in m for m in fixed_merchants)
    assert any("Synchrony" in m for m in fixed_merchants)
    assert not any("Autopay" in m or "Chase" in m for m in fixed_merchants)
    # discretionary spend this month so far = 62.40 only
    assert Decimal(power["spent_so_far"]) == Decimal("62.40")

    obs = (await client.get("/obligations")).json()
    assert len(obs) == 1
    assert obs[0]["months_left"] == 9
    assert Decimal(obs[0]["monthly_payment"]) == Decimal("135.00")

    networth = (await client.get("/networth")).json()
    # 4200 + 10000 - (350 + 1215)
    assert Decimal(networth["net_worth"]) == Decimal("12635.00")
