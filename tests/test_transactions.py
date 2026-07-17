from datetime import date

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moneta.api import create_app
from moneta.models import AccountType, SeriesStatus, TransferLink
from moneta.views.power import power_report
from moneta.views.transactions import transactions_report
from tests.factories import make_account, make_series, make_txn


async def test_plain_spend_is_counted(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session, checking, amount_cents=-2500, merchant="Coffee Shop", posted_on=date(2026, 7, 5)
    )
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    assert len(rows) == 1
    row = rows[0]
    assert row.counted_in_spend is True
    assert row.excluded_because is None
    assert row.amount_cents == -2500
    assert row.link is None
    assert row.series is None


async def test_inflow_excluded(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session, checking, amount_cents=300000, merchant="Acme Payroll", posted_on=date(2026, 7, 5)
    )
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    row = rows[0]
    assert row.counted_in_spend is False
    assert row.excluded_because == "inflow"
    assert row.link is None


async def test_internal_transfer_both_legs(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session, checking, amount_cents=-10000, posted_on=date(2026, 7, 5), description="TO SAVINGS"
    )
    inn = await make_txn(
        session,
        savings,
        amount_cents=10000,
        posted_on=date(2026, 7, 5),
        description="FROM CHECKING",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    by_id = {r.id: r for r in rows}
    outflow_row = by_id[out.id]
    inflow_row = by_id[inn.id]
    assert outflow_row.link == "internal"
    assert outflow_row.excluded_because == "transfer"
    assert outflow_row.counted_in_spend is False
    assert inflow_row.link == "internal"
    assert inflow_row.excluded_because == "inflow"
    assert inflow_row.counted_in_spend is False


async def test_loan_payment_outflow_excluded(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(session, type=AccountType.loan)
    out = await make_txn(
        session,
        checking,
        amount_cents=-13500,
        merchant="Synchrony Bank",
        posted_on=date(2026, 7, 5),
    )
    inn = await make_txn(
        session, loan, amount_cents=13500, merchant="Synchrony Bank", posted_on=date(2026, 7, 5)
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    outflow_row = next(r for r in rows if r.id == out.id)
    assert outflow_row.link == "loan_payment"
    assert outflow_row.excluded_because == "loan payment"
    assert outflow_row.counted_in_spend is False


async def test_cc_payment_outflow_excluded(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    credit = await make_account(session, type=AccountType.credit)
    out = await make_txn(
        session,
        checking,
        amount_cents=-50000,
        merchant="Chase Card Payment",
        posted_on=date(2026, 7, 5),
    )
    inn = await make_txn(
        session,
        credit,
        amount_cents=50000,
        merchant="Chase Card Payment",
        posted_on=date(2026, 7, 5),
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    outflow_row = next(r for r in rows if r.id == out.id)
    assert outflow_row.link == "cc_payment"
    assert outflow_row.excluded_because == "credit-card payment"
    assert outflow_row.counted_in_spend is False


async def test_financing_mode_credit_card_payment_is_loan_payment(session: AsyncSession) -> None:
    """A financing-mode credit account is loan-like — its payment must classify as
    'loan payment', not 'credit-card payment' (mirrors power.py's cc_series rule)."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(session, type=AccountType.credit, financing_mode=True)
    out = await make_txn(
        session, checking, amount_cents=-30000, posted_on=date(2026, 7, 5), merchant="Synchrony"
    )
    inn = await make_txn(
        session, card, amount_cents=30000, posted_on=date(2026, 7, 5), merchant="Synchrony"
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.flush()
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    outflow_row = next(r for r in rows if r.id == out.id)
    assert outflow_row.link == "loan_payment"
    assert outflow_row.excluded_because == "loan payment"


async def test_active_series_txn_is_fixed_cost(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    netflix = await make_series(session, merchant="Netflix", expected_cents=-1599)
    await make_txn(
        session,
        checking,
        amount_cents=-1599,
        merchant="Netflix",
        posted_on=date(2026, 7, 5),
        series_id=netflix.id,
    )
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    row = rows[0]
    assert row.series == "Netflix"
    assert row.series_status == SeriesStatus.active
    assert row.series_discretionary is False
    assert row.counted_in_spend is False
    assert row.excluded_because == "fixed cost (series Netflix)"


async def test_ended_series_txn_counted_as_spend(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    dead_gym = await make_series(
        session, merchant="Dead Gym", expected_cents=-4999, status=SeriesStatus.ended
    )
    await make_txn(
        session,
        checking,
        amount_cents=-4999,
        merchant="Dead Gym",
        posted_on=date(2026, 7, 5),
        series_id=dead_gym.id,
    )
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    row = rows[0]
    assert row.counted_in_spend is True
    assert row.excluded_because is None
    assert row.series == "Dead Gym"
    assert row.series_status == SeriesStatus.ended


async def test_discretionary_series_txn_counted_as_spend(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    dining = await make_series(
        session, merchant="Dining Out", expected_cents=-3886, discretionary=True
    )
    await make_txn(
        session,
        checking,
        amount_cents=-3886,
        merchant="Dining Out",
        posted_on=date(2026, 7, 5),
        series_id=dining.id,
    )
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    row = rows[0]
    assert row.counted_in_spend is True
    assert row.excluded_because is None
    assert row.series == "Dining Out"
    assert row.series_discretionary is True


async def test_non_spend_account_type_excluded(session: AsyncSession) -> None:
    brokerage = await make_account(session, type=AccountType.brokerage)
    await make_txn(session, brokerage, amount_cents=-5000, posted_on=date(2026, 7, 5))
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    row = rows[0]
    assert row.counted_in_spend is False
    assert row.excluded_because == "non-spend account"


async def test_foreign_currency_excluded(session: AsyncSession) -> None:
    await make_account(
        session, type=AccountType.checking, currency="USD"
    )  # establishes USD as primary
    eur = await make_account(session, type=AccountType.checking, currency="EUR")
    await make_txn(session, eur, amount_cents=-7000, posted_on=date(2026, 7, 5))
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    row = rows[0]
    assert row.counted_in_spend is False
    assert row.excluded_because == "foreign currency"


async def test_account_id_filter(session: AsyncSession) -> None:
    a = await make_account(session, type=AccountType.checking)
    b = await make_account(session, type=AccountType.checking)
    await make_txn(session, a, amount_cents=-1000, posted_on=date(2026, 7, 5))
    await make_txn(session, b, amount_cents=-2000, posted_on=date(2026, 7, 5))
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31), account_id=a.id)
    assert len(rows) == 1
    assert rows[0].amount_cents == -1000


async def test_merchant_filter_is_case_insensitive_substring(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session, checking, amount_cents=-1500, merchant="Netflix", posted_on=date(2026, 7, 5)
    )
    await make_txn(
        session, checking, amount_cents=-900, merchant="Spotify", posted_on=date(2026, 7, 5)
    )
    rows = await transactions_report(
        session, date(2026, 7, 1), date(2026, 7, 31), merchant="netFLIX"
    )
    assert len(rows) == 1
    assert rows[0].merchant == "Netflix"


async def test_merchant_filter_escapes_like_metacharacters(session: AsyncSession) -> None:
    """'AT_T' must be treated literally — '_' is a LIKE single-char wildcard and would
    otherwise also match 'ATXT'."""
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session, checking, amount_cents=-1500, merchant="AT_T Wireless", posted_on=date(2026, 7, 5)
    )
    await make_txn(
        session, checking, amount_cents=-900, merchant="ATXT", posted_on=date(2026, 7, 5)
    )
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31), merchant="AT_T")
    assert len(rows) == 1
    assert rows[0].merchant == "AT_T Wireless"


async def test_date_range_filter(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(session, checking, amount_cents=-1000, posted_on=date(2026, 6, 30))
    await make_txn(session, checking, amount_cents=-2000, posted_on=date(2026, 7, 1))
    await make_txn(session, checking, amount_cents=-3000, posted_on=date(2026, 7, 31))
    await make_txn(session, checking, amount_cents=-4000, posted_on=date(2026, 8, 1))
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    assert {r.amount_cents for r in rows} == {-2000, -3000}


async def test_newest_first_ordering(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    early = await make_txn(session, checking, amount_cents=-1000, posted_on=date(2026, 7, 1))
    late = await make_txn(session, checking, amount_cents=-2000, posted_on=date(2026, 7, 10))
    # same day, different insertion order — id desc breaks the tie
    same_day_first = await make_txn(
        session, checking, amount_cents=-3000, posted_on=date(2026, 7, 10)
    )
    same_day_second = await make_txn(
        session, checking, amount_cents=-4000, posted_on=date(2026, 7, 10)
    )
    rows = await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    assert [r.id for r in rows] == [
        same_day_second.id,
        same_day_first.id,
        late.id,
        early.id,
    ]


async def test_endpoint_defaults_to_current_month_and_pins_signed_int(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    today = date.today()
    await make_txn(session, checking, amount_cents=-1234, posted_on=today)
    await session.commit()

    app = create_app(sessionmaker, adapter=None, llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/transactions")

    assert r.status_code == 200
    body = r.json()
    assert len(body["transactions"]) == 1
    assert isinstance(body["transactions"][0]["amount_cents"], int)
    assert body["transactions"][0]["amount_cents"] == -1234  # signed, per design §1
    assert body["counted_total_cents"] == 1234
    assert body["through_today_cents"] == 1234


async def test_endpoint_filters_pass_through(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    checking = await make_account(session, type=AccountType.checking)
    await make_txn(
        session, checking, amount_cents=-1500, merchant="Netflix", posted_on=date(2026, 7, 5)
    )
    other = await make_account(session, type=AccountType.checking)
    await make_txn(
        session, other, amount_cents=-900, merchant="Spotify", posted_on=date(2026, 7, 5)
    )
    await session.commit()

    app = create_app(sessionmaker, adapter=None, llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            "/transactions",
            params={
                "start": "2026-07-01",
                "end": "2026-07-31",
                "account_id": checking.id,
                "merchant": "net",
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert len(body["transactions"]) == 1
    assert body["transactions"][0]["merchant"] == "Netflix"


async def test_power_and_transactions_agree_on_spent_so_far(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Parity: power.spent_so_far_cents and /transactions' through_today_cents are
    built from the same spend_reason predicate (design 2026-07-16 §2/§3) and must
    always agree for a range that covers today."""
    checking = await make_account(session, type=AccountType.checking)
    credit = await make_account(session, type=AccountType.credit)
    loan = await make_account(session, type=AccountType.loan)
    brokerage = await make_account(session, type=AccountType.brokerage)
    netflix = await make_series(session, merchant="Netflix", expected_cents=-1599)

    today = date.today()
    month_start = today.replace(day=1)
    # plain spend — counted
    await make_txn(session, checking, amount_cents=-2500, posted_on=month_start)
    # inflow — excluded
    await make_txn(session, checking, amount_cents=300000, posted_on=month_start)
    # active non-discretionary series — excluded (fixed cost)
    await make_txn(
        session,
        checking,
        amount_cents=-1599,
        merchant="Netflix",
        posted_on=month_start,
        series_id=netflix.id,
    )
    # non-spend account — excluded
    await make_txn(session, brokerage, amount_cents=-5000, posted_on=month_start)
    # cc payment link — outflow excluded (purchase counted instead), inflow excluded (inflow)
    cc_out = await make_txn(session, checking, amount_cents=-8000, posted_on=month_start)
    cc_in = await make_txn(session, credit, amount_cents=8000, posted_on=month_start)
    session.add(
        TransferLink(outflow_id=cc_out.id, inflow_id=cc_in.id, confidence=1.0, method="rule")
    )
    # loan payment link — outflow excluded (counted as a fixed cost line instead)
    loan_out = await make_txn(session, checking, amount_cents=-13500, posted_on=month_start)
    loan_in = await make_txn(session, loan, amount_cents=13500, posted_on=month_start)
    session.add(
        TransferLink(outflow_id=loan_out.id, inflow_id=loan_in.id, confidence=1.0, method="rule")
    )
    await session.commit()

    power = await power_report(session, today=today)

    app = create_app(sessionmaker, adapter=None, llm=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/transactions")
    body = r.json()

    assert power.spent_so_far_cents == 2500  # only the plain checking spend
    assert body["through_today_cents"] == power.spent_so_far_cents
