"""A pass-through merchant: someone else's money moving through the user's account
(a partner's rent they pay back), never the user's spending, income or bill."""

from datetime import date
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from moneta.api import create_app
from moneta.cli.main import app as cli_app
from moneta.models import (
    AccountType,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    Transaction,
    pass_through_item,
    recurring_cluster_item,
)
from moneta.pipelines.normalize import normalize_merchants, renormalize_merchants
from moneta.pipelines.recurring import detect_recurring, rule_pass_through
from moneta.pipelines.review import apply_resolution
from moneta.queries import pass_through_merchants
from moneta.views.cashflow import accrual_by_month, accrual_inflows, accrual_spend, cash_out
from moneta.views.power import power_report
from moneta.views.transactions import transactions_report
from tests.factories import make_account, make_price_change_item, make_series, make_txn

RENT = "Acme Lofts"


async def _rent_month(session: AsyncSession) -> tuple[Transaction, Transaction, Transaction]:
    """One month: the partner pays the user back, the user pays the rent, plus a coffee."""
    checking = await make_account(session, type=AccountType.checking)
    back = await make_txn(
        session,
        checking,
        amount_cents=150000,
        merchant="P2P From Partner",
        posted_on=date(2026, 7, 2),
    )
    rent = await make_txn(
        session, checking, amount_cents=-150800, merchant=RENT, posted_on=date(2026, 7, 3)
    )
    coffee = await make_txn(
        session, checking, amount_cents=-500, merchant="Coffee Shop", posted_on=date(2026, 7, 4)
    )
    return back, rent, coffee


async def test_newest_ruling_per_merchant_wins(session: AsyncSession) -> None:
    session.add(pass_through_item(RENT, True))
    session.add(pass_through_item("Other Co", True))
    session.add(pass_through_item("Other Co", False))
    await session.flush()
    assert await pass_through_merchants(session) == frozenset({RENT})


async def test_trust_view_and_power_leave_pass_through_out_of_spend(
    session: AsyncSession,
) -> None:
    _back, rent, coffee = await _rent_month(session)
    refund = await make_txn(
        session,
        await make_account(session, type=AccountType.checking),
        amount_cents=2000,
        merchant=RENT,
        posted_on=date(2026, 7, 5),
    )
    session.add(pass_through_item(RENT, True))
    await session.flush()

    rows = {
        r.id: r for r in await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    }
    assert rows[rent.id].excluded_because == "pass-through"
    assert rows[rent.id].counted_in_spend is False
    assert rows[refund.id].excluded_because == "pass-through"
    assert rows[coffee.id].counted_in_spend is True
    report = await power_report(session, date(2026, 7, 10))
    assert report.spent_so_far_cents == 500


async def test_clearing_the_ruling_counts_the_merchant_again(session: AsyncSession) -> None:
    _back, rent, _coffee = await _rent_month(session)
    session.add(pass_through_item(RENT, True))
    session.add(pass_through_item(RENT, False))
    await session.flush()

    rows = {
        r.id: r for r in await transactions_report(session, date(2026, 7, 1), date(2026, 7, 31))
    }
    assert rows[rent.id].counted_in_spend is True
    assert (await power_report(session, date(2026, 7, 10))).spent_so_far_cents == 151300


async def test_cashflow_and_history_leave_pass_through_out(session: AsyncSession) -> None:
    await _rent_month(session)
    session.add(pass_through_item(RENT, True))
    await session.flush()
    start, end = date(2026, 7, 1), date(2026, 7, 31)

    assert await accrual_spend(session, start, end) == 500
    assert await cash_out(session, start, end) == 500
    # the partner's repayment is not the merchant's, so it stays an other inflow
    assert await accrual_inflows(session, start, end) == 150000
    (july,) = await accrual_by_month(session, 1, date(2026, 7, 20))
    assert july.spend_cents == 500
    assert july.other_inflows_cents == 150000
    assert july.income_cents == 0


async def test_a_pass_through_merchant_never_becomes_a_bill(session: AsyncSession) -> None:
    checking = await make_account(session)
    for month in (4, 5, 6):
        await make_txn(
            session, checking, amount_cents=-150800, merchant=RENT, posted_on=date(2026, month, 3)
        )
    session.add(pass_through_item(RENT, True))
    await session.commit()

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    assert stats.new_series == 0
    assert (await session.execute(select(Transaction.series_id))).scalars().all() == [None] * 3


async def test_ruling_untags_and_ends_an_existing_series_and_retires_its_question(
    session: AsyncSession,
) -> None:
    checking = await make_account(session)
    series = await make_series(
        session, merchant=RENT, expected_cents=-150800, next_expected_on=date(2026, 7, 3)
    )
    for month in (4, 5, 6):
        await make_txn(
            session,
            checking,
            amount_cents=-150800,
            merchant=RENT,
            posted_on=date(2026, month, 3),
            series_id=series.id,
        )
    question = recurring_cluster_item(RENT, "outflow", None)
    session.add(question)
    session.add(pass_through_item(RENT, True))
    await session.commit()

    await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    await session.refresh(series)
    assert series.status == SeriesStatus.ended
    assert (await session.execute(select(Transaction.series_id))).scalars().all() == [None] * 3
    await session.refresh(question)
    assert question.status == ReviewStatus.resolved
    assert question.resolution is not None
    assert question.resolution["resolved_by"] == "reconciliation"


async def test_ruling_follows_a_merchant_rename(session: AsyncSession) -> None:
    acct = await make_account(session)
    for raw in ("ACME LOFTS LLC RIVERTON TX", "APLPAY ACME LOFTS"):
        await make_txn(session, acct, description=raw)
    await normalize_merchants(session, llm=None)  # "Acme Lofts Riverton", "Acme Lofts"
    session.add(pass_through_item("Acme Lofts Riverton", True))
    await session.commit()

    await renormalize_merchants(session)

    assert await pass_through_merchants(session) == frozenset({RENT})


async def test_power_drops_a_ruled_merchants_series_before_any_sync(
    session: AsyncSession,
) -> None:
    await make_series(session, merchant=RENT, expected_cents=-150800)
    await make_series(session, merchant=RENT, direction="inflow", expected_cents=150000)
    session.add(pass_through_item(RENT, True))
    await session.flush()

    report = await power_report(session, date(2026, 7, 10))

    assert report.monthly_income_cents == 0
    assert report.total_fixed_cents == 0
    assert report.upcoming == []


async def test_detection_ends_a_pass_through_series_reactivated_by_hand(
    session: AsyncSession,
) -> None:
    series = await make_series(session, merchant=RENT, expected_cents=-150800)
    session.add(pass_through_item(RENT, True))
    await session.commit()

    await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    await session.refresh(series)
    assert series.status == SeriesStatus.ended


async def test_clearing_reopens_only_the_questions_the_ruling_retired(
    session: AsyncSession,
) -> None:
    series = await make_series(session, merchant=RENT, expected_cents=-150800)
    bill_question = recurring_cluster_item(RENT, "outflow", None)
    price_question = make_price_change_item(series.id)
    answered = recurring_cluster_item(RENT, "inflow", None)
    answered.status = ReviewStatus.resolved
    answered.resolution = {"is_recurring": True, "resolved_by": "manual"}
    session.add_all([bill_question, price_question, answered])
    await session.flush()

    await rule_pass_through(session, [RENT], True, date(2026, 7, 1))
    assert bill_question.status == price_question.status == ReviewStatus.resolved
    await rule_pass_through(session, [RENT], False, date(2026, 7, 2))

    for reopened in (bill_question, price_question):
        assert reopened.status == ReviewStatus.open
        assert reopened.resolution is None
    assert answered.resolution == {"is_recurring": True, "resolved_by": "manual"}


async def test_clearing_after_a_sync_leaves_an_ended_series_price_question_closed(
    session: AsyncSession,
) -> None:
    series = await make_series(session, merchant=RENT, expected_cents=-150800)
    price_question = make_price_change_item(series.id)
    session.add(price_question)
    await session.flush()
    await rule_pass_through(session, [RENT], True, date(2026, 7, 1))
    series.status = SeriesStatus.ended  # what the next sync does

    await rule_pass_through(session, [RENT], False, date(2026, 7, 2))

    # events asks afresh once the series revives
    assert price_question.status == ReviewStatus.resolved


async def test_a_merchant_answer_carries_the_ruling_to_the_new_name(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="ACME LOFTS 0034 PMT", merchant="Acme Lofts 0034")
    for raw in ("ACME LOFTS 0035 PMT", "ACME LOFTS 0035 WEB"):
        await make_txn(session, acct, description=raw, merchant="Acme Lofts 0035")
    session.add_all(
        [pass_through_item("Acme Lofts 0034", True), pass_through_item("Acme Lofts 0035", True)]
    )
    renamed, partly = (
        ReviewItem(
            kind=ReviewKind.merchant,
            question="Merchant?",
            payload={"descriptor": raw, "fallback": raw.title()},
        )
        for raw in ("ACME LOFTS 0034 PMT", "ACME LOFTS 0035 PMT")
    )
    session.add_all([renamed, partly])
    await session.flush()

    await apply_resolution(session, renamed, {"merchant": RENT})
    # another descriptor still carries the old name, so the ruling stays with it
    await apply_resolution(session, partly, {"merchant": "Riverton Rent"})

    assert await pass_through_merchants(session) == frozenset({RENT, "Acme Lofts 0035"})


async def test_endpoint_rules_by_any_spelling_and_retires_open_questions_at_once(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        checking = await make_account(session, source="simplefin")
        alias = await make_account(session, source="plaid", canonical_account_id=checking.id)
        series = await make_series(session, merchant=RENT, expected_cents=-150800)
        root = await make_txn(
            session, checking, amount_cents=-150800, merchant=RENT, series_id=series.id
        )
        # a name only a suppressed provider copy carries is not a merchant txns shows
        await make_txn(
            session,
            alias,
            amount_cents=-150800,
            merchant="Acme Lofts Copy",
            duplicate_of_id=root.id,
        )
        bill_question = recurring_cluster_item(RENT, "outflow", None)
        price_question = make_price_change_item(series.id)
        session.add_all([bill_question, price_question])
        await session.commit()
        series_id = series.id
        question_ids = (bill_question.id, price_question.id)

    def fixed_cents() -> Any:
        async def read() -> int:
            async with sessionmaker() as session:
                return (await power_report(session, date.today())).total_fixed_cents

        return read()

    transport = httpx.ASGITransport(app=create_app(sessionmaker, adapters=[], llm=None))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert await fixed_cents() == 150800
        r = await client.post(
            "/merchants/pass-through", json={"merchant": " acme lofts ", "pass_through": True}
        )
        assert r.status_code == 200
        assert r.json() == {"merchant": RENT, "pass_through": True}
        assert await fixed_cents() == 0
        for unknown in ("Nobody", "Acme Lofts Copy"):
            missing = await client.post(
                "/merchants/pass-through", json={"merchant": unknown, "pass_through": True}
            )
            assert missing.status_code == 404

        async with sessionmaker() as session:
            for question_id in question_ids:
                question = await session.get(ReviewItem, question_id)
                assert question is not None and question.status == ReviewStatus.resolved
            # the series is left to the next sync, so clearing before one restores it
            kept = await session.get(RecurringSeries, series_id)
            assert kept is not None and kept.status == SeriesStatus.active

        r = await client.post(
            "/merchants/pass-through", json={"merchant": RENT, "pass_through": False}
        )
        assert r.json() == {"merchant": RENT, "pass_through": False}
        assert await fixed_cents() == 150800
        async with sessionmaker() as session:
            for question_id in question_ids:
                question = await session.get(ReviewItem, question_id)
                assert question is not None and question.status == ReviewStatus.open

    async with sessionmaker() as session:
        assert await pass_through_merchants(session) == frozenset()
        rulings = (
            await session.execute(
                select(ReviewItem)
                .where(ReviewItem.kind == ReviewKind.pass_through)
                .order_by(ReviewItem.id)
            )
        ).scalars()
        assert [(i.status, i.resolution) for i in rulings] == [
            (ReviewStatus.resolved, {"pass_through": True, "resolved_by": "manual"}),
            (ReviewStatus.resolved, {"pass_through": False, "resolved_by": "manual"}),
        ]


runner = CliRunner()


def _record(monkeypatch: Any) -> list[tuple[str, str, Any]]:
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, json_body))
        return {"merchant": RENT, "pass_through": bool(json_body and json_body["pass_through"])}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    return calls


def test_txns_pass_through_flags_post_the_ruling(monkeypatch: Any) -> None:
    calls = _record(monkeypatch)
    on = runner.invoke(cli_app, ["txns", "--pass-through", "acme lofts"])
    off = runner.invoke(cli_app, ["txns", "--clear-pass-through", "acme lofts"])
    assert on.exit_code == 0 and off.exit_code == 0
    assert calls == [
        ("POST", "/merchants/pass-through", {"merchant": "acme lofts", "pass_through": True}),
        ("POST", "/merchants/pass-through", {"merchant": "acme lofts", "pass_through": False}),
    ]
    assert "Acme Lofts is a pass-through" in on.output
    assert "Acme Lofts counts again" in off.output


def test_txns_pass_through_flags_exclude_each_other_and_json(monkeypatch: Any) -> None:
    calls = _record(monkeypatch)
    both = runner.invoke(cli_app, ["txns", "--pass-through", "a", "--clear-pass-through", "a"])
    with_json = runner.invoke(cli_app, ["txns", "--pass-through", "a", "--json"])
    assert both.exit_code == 1 and with_json.exit_code == 1
    assert calls == []
