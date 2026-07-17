from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO
from moneta.api import create_app
from moneta.pipelines.run import run_sync
from moneta.views.financing import compute_obligations
from moneta.views.networth import net_worth_report
from moneta.views.power import power_report
from tests.conftest import FakeAdapter

# The API endpoints resolve date.today() at request time, so all snapshot
# dates must be computed relative to today or the scenario silently ages out
# (paychecks fall outside the detection window, current-month spend goes to 0).
TODAY = date.today()


def _month_start(today: date, months_back: int) -> date:
    """First of the month `months_back` months before today's month."""
    y, m = today.year, today.month - months_back
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _acct(id_: str, name: str, balance: str, balance_date: date) -> AccountDTO:
    return AccountDTO(
        id=id_,
        name=name,
        org_name="Bank",
        currency="USD",
        balance=Decimal(balance),
        balance_date=balance_date,
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


def _build_scenario(today: date) -> Snapshot:
    """Same scenario shape for every `today`: 5 biweekly paychecks ending today,
    3 months of rent/netflix/synchrony-loan-payment anchored to each month's 1st
    (so the newest occurrence is always <= today, even when today IS the 1st),
    2 months of CC autopay, and one discretionary restaurant charge today.
    Structural results (income, fixed costs, spent-so-far, obligations, net worth)
    are today-invariant — parametrizing `today` pins calendar-edge reproductions
    (design 2026-07-16 §3) without changing what the scenario asserts.
    """

    def month_start(months_back: int) -> date:
        return _month_start(today, months_back)

    return Snapshot(
        accounts=[
            _acct("CHK", "Premier Checking", "4200.00", today),
            _acct("SAV", "Online Savings", "10000.00", today),
            _acct("CC", "Freedom Credit Card", "-350.00", today),
            _acct("SYN", "Synchrony Financing Loan", "-1215.00", today),
        ],
        transactions=[
            # biweekly paychecks, most recent today
            *[
                _txn(
                    f"PAY{i}", "CHK", today - timedelta(days=14 * i), "2500.00", "ACME CORP PAYROLL"
                )
                for i in range(5)
            ],
            # monthly rent: current month and two prior.
            *[
                _txn(f"RENT{n}", "CHK", month_start(n), "-1800.00", "OAKWOOD PROPERTIES RENT")
                for n in (2, 1, 0)
            ],
            # netflix on the credit card: three monthly charges ending this month
            # (recurring detection needs >= 3 occurrences to form a series)
            *[_txn(f"NFLX{n}", "CC", month_start(n), "-15.99", "NETFLIX.COM") for n in (2, 1, 0)],
            # cc payments checking -> cc, same-day pairs in the two prior months
            *[
                t
                for n in (2, 1)
                for t in (
                    _txn(
                        f"CCP{n}",
                        "CHK",
                        month_start(n) + timedelta(days=19),
                        "-215.99",
                        "CHASE CARD AUTOPAY PAYMENT",
                    ),
                    _txn(
                        f"CCR{n}",
                        "CC",
                        month_start(n) + timedelta(days=19),
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
                    _txn(f"SYP{n}", "CHK", month_start(n), "-135.00", "SYNCHRONY BANK PAYMENT"),
                    _txn(f"SYR{n}", "SYN", month_start(n), "135.00", "SYNCHRONY BANK PAYMENT"),
                )
            ],
            # the only discretionary spend this month
            _txn("DIN1", "CC", today, "-62.40", "GOOD RESTAURANT"),
        ],
        holdings=[],
    )


SNAPSHOT = _build_scenario(TODAY)


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(sessionmaker, adapters=[FakeAdapter(SNAPSHOT)], llm=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_full_pipeline_power_and_obligations(client: httpx.AsyncClient) -> None:
    assert (await client.post("/sync")).status_code == 200

    power = (await client.get("/power")).json()
    # income: 2500 biweekly -> 5416.67/mo
    assert power["monthly_income_cents"] == 541667
    fixed_merchants = {line["merchant"] for line in power["fixed_costs"]}
    # rent + netflix + synchrony are fixed; CC payment series must NOT be
    assert any("Rent" in m or "Oakwood" in m for m in fixed_merchants)
    assert any("Netflix" in m for m in fixed_merchants)
    assert any("Synchrony" in m for m in fixed_merchants)
    assert not any("Autopay" in m or "Chase" in m for m in fixed_merchants)
    # discretionary spend this month so far = 62.40 only
    assert power["spent_so_far_cents"] == 6240

    obs = (await client.get("/obligations")).json()
    assert len(obs) == 1
    assert obs[0]["months_left"] == 9
    assert obs[0]["monthly_payment_cents"] == 13500

    networth = (await client.get("/networth")).json()
    # 4200 + 10000 - (350 + 1215)
    assert networth["net_worth_cents"] == 1263500


# Fixed `today` anchors reproducing calendar edge cases deterministically (design
# 2026-07-16 §3) — unlike the live-today test above, these drive the pipeline/view
# functions directly with an explicit `today` instead of the /sync,/power,/networth,
# /obligations endpoints, which resolve date.today() at request time and so cannot be
# pinned to a historical or future date from the test.
@pytest.mark.parametrize(
    "today",
    [date(2026, 1, 1), date(2028, 2, 29), date(2026, 7, 31)],
    ids=["year-boundary", "leap-day", "month-end"],
)
async def test_full_pipeline_power_and_obligations_at_fixed_today(
    session: AsyncSession, today: date
) -> None:
    snapshot = _build_scenario(today)
    await run_sync(session, [FakeAdapter(snapshot)], llm=None, today=today)

    power = await power_report(session, today=today)
    assert power.monthly_income_cents == 541667
    fixed_merchants = {line.merchant for line in power.fixed_costs}
    assert any("Rent" in m or "Oakwood" in m for m in fixed_merchants)
    assert any("Netflix" in m for m in fixed_merchants)
    assert any("Synchrony" in m for m in fixed_merchants)
    assert not any("Autopay" in m or "Chase" in m for m in fixed_merchants)
    assert power.spent_so_far_cents == 6240

    obligations = await compute_obligations(session, today=today)
    assert len(obligations) == 1
    assert obligations[0].months_left == 9
    assert obligations[0].monthly_payment_cents == 13500

    networth = await net_worth_report(session)
    assert networth.net_worth_cents == 1263500
