from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO
from moneta.api import create_app


def _acct(id_: str, name: str, balance: str) -> AccountDTO:
    return AccountDTO(
        id=id_,
        name=name,
        org_name="Bank",
        currency="USD",
        balance=Decimal(balance),
        balance_date=date(2026, 7, 7),
    )


def _txn(id_: str, acct: str, day: tuple[int, int], amount: str, desc: str) -> TransactionDTO:
    return TransactionDTO(
        id=id_,
        account_id=acct,
        posted_on=date(2026, *day),
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
        # biweekly paychecks
        *[
            _txn(f"PAY{i}", "CHK", d, "2500.00", "ACME CORP PAYROLL")
            for i, d in enumerate([(5, 1), (5, 15), (5, 29), (6, 12), (6, 26)])
        ],
        # monthly rent
        *[
            _txn(f"RENT{m}", "CHK", (m, 1), "-1800.00", "OAKWOOD PROPERTIES RENT")
            for m in (5, 6, 7)
        ],
        # netflix on the credit card (three monthly charges so a recurring
        # series can form; day 2 keeps every occurrence safely in the past
        # relative to "today")
        *[_txn(f"NFLX{m}", "CC", (m, 2), "-15.99", "NETFLIX.COM") for m in (5, 6, 7)],
        # cc payments checking -> cc
        *[
            t
            for m in (5, 6)
            for t in (
                _txn(f"CCP{m}", "CHK", (m, 20), "-215.99", "CHASE CARD AUTOPAY PAYMENT"),
                _txn(f"CCR{m}", "CC", (m, 20), "215.99", "AUTOPAY PAYMENT THANK YOU"),
            )
        ],
        # synchrony payments checking -> loan
        *[
            t
            for m in (5, 6, 7)
            for t in (
                _txn(f"SYP{m}", "CHK", (m, 5), "-135.00", "SYNCHRONY BANK PAYMENT"),
                _txn(f"SYR{m}", "SYN", (m, 5), "135.00", "SYNCHRONY BANK PAYMENT"),
            )
        ],
        # discretionary spend in july
        _txn("DIN1", "CC", (7, 3), "-62.40", "GOOD RESTAURANT"),
    ],
    holdings=[],
)


class FakeAdapter:
    async def fetch(self, since: date | None = None) -> Snapshot:
        return SNAPSHOT


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(sessionmaker, adapter=FakeAdapter(), llm=None)
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
    # discretionary spend in july so far = 62.40 only
    assert Decimal(power["spent_so_far"]) == Decimal("62.40")

    obs = (await client.get("/obligations")).json()
    assert len(obs) == 1
    assert obs[0]["months_left"] == 9
    assert Decimal(obs[0]["monthly_payment"]) == Decimal("135.00")

    networth = (await client.get("/networth")).json()
    # 4200 + 10000 - (350 + 1215)
    assert Decimal(networth["net_worth"]) == Decimal("12635.00")
