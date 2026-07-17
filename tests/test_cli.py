import asyncio
import json
from calendar import monthrange
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from moneta.cli.main import app
from moneta.db import init_db, make_sessionmaker
from moneta.models import (
    AccountType,
    Cadence,
    Direction,
    EventKind,
    ReviewItem,
    SeriesEvent,
    SyncRun,
    TransferLink,
)
from tests.factories import (
    make_account,
    make_price_change_item,
    make_series,
    make_series_event,
    make_txn,
)

runner = CliRunner()


def _isolate(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("MONETA_API_URL", raising=False)
    monkeypatch.delenv("MONETA_SIMPLEFIN_ACCESS_URL", raising=False)


def _seed_db[T](tmp_path: Path, populate: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Populate the file-backed DB the in-process CLI reads; owns the engine lifecycle."""

    async def _run() -> T:
        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            result = await populate(session)
            await session.commit()
        await engine.dispose()
        return result

    return asyncio.run(_run())


def test_power_runs_in_process(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    assert "Spending power" in result.output


def test_networth_runs(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["networth"])
    assert result.exit_code == 0
    assert "Net worth" in result.output


def test_sync_without_setup_fails_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "simplefin" in result.output
    assert "plaid" in result.output


def test_sync_full_flag_requests_full_sync(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, params))
        if path == "/sync":
            return {
                "ingest": {"new_transactions": 0},
                "transfers": {"linked": 0},
                "recurring": {"new_series": 0},
                "events": 0,
                "auto_resolved": 0,
                "verify": {"verified": 0, "flagged": 0},
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync", "--full"])
    assert result.exit_code == 0
    full_call = calls[0]
    assert full_call == ("POST", "/sync", {"full": True})
    calls.clear()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    plain_call = calls[0]
    assert plain_call == ("POST", "/sync", None)


def test_import_vesting(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    csv_file = tmp_path / "vest.csv"
    csv_file.write_text("symbol,vested_quantity,unvested_quantity\nACME,40,60\n")
    result = runner.invoke(app, ["import", "vesting", str(csv_file)])
    assert result.exit_code == 0
    assert "0" in result.output  # updated count (no holdings in fresh db)


def test_set_promo_invalid_date_fails_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["accounts", "--set-promo", "1", "not-a-date"])
    assert result.exit_code == 1
    assert "invalid date" in result.output
    assert "Traceback" not in result.output


def test_accounts_set_financing_flag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, json_body))
        if method == "GET":
            return []
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["accounts", "--set-financing", "3", "true"])
    assert result.exit_code == 0
    assert ("PATCH", "/accounts/3", {"financing_mode": True}) in calls
    assert "Traceback" not in result.output


def test_accounts_set_financing_flag_false(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, json_body))
        if method == "GET":
            return []
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["accounts", "--set-financing", "3", "false"])
    assert result.exit_code == 0
    assert ("PATCH", "/accounts/3", {"financing_mode": False}) in calls


def test_accounts_set_financing_invalid_value_fails_cleanly(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        raise AssertionError("request must not be called on invalid input")

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["accounts", "--set-financing", "3", "maybe"])
    assert result.exit_code == 1
    assert "Error" in result.output
    assert "Traceback" not in result.output


def test_accounts_shows_financing_marker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return [
            {
                "id": 1,
                "name": "Big Bank Card",
                "org_name": "Big Bank",
                "type": "credit",
                "balance_cents": -50000,
                "promo_expires_on": None,
                "financing_mode": True,
            }
        ]

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 0
    assert "credit (financing)" in result.output


def test_recurring_end_option_ends_series(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> int:
        return (await make_series(session, next_expected_on=date(2026, 7, 15))).id

    series_id = _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["recurring", "--end", str(series_id)])
    assert result.exit_code == 0
    assert "ended" in result.output.lower()
    assert "Traceback" not in result.output

    result = runner.invoke(app, ["recurring"])
    assert "ended" in result.output


def test_recurring_not_a_bill_flag_posts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, json_body, params))
        if method == "GET":
            return []
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["recurring", "--not-a-bill", "4"])
    assert result.exit_code == 0
    assert ("POST", "/recurring/4/not-a-bill", None, None) in calls
    assert "not-a-bill" in result.output
    assert "Traceback" not in result.output


def test_recurring_habit_and_re_review_flags_post(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, json_body, params))
        if method == "GET":
            return []
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)

    result = runner.invoke(app, ["recurring", "--habit", "7"])
    assert result.exit_code == 0
    assert ("POST", "/recurring/7/habit", None, None) in calls
    assert "habit" in result.output

    calls.clear()
    result = runner.invoke(app, ["recurring", "--re-review", "9"])
    assert result.exit_code == 0
    assert ("POST", "/recurring/9/re-review", None, None) in calls
    assert "reopened" in result.output


def test_recurring_overrule_flags_mutually_exclusive(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path))
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["recurring", "--not-a-bill", "4", "--habit", "5"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    assert "Traceback" not in result.output
    assert calls == []

    result = runner.invoke(app, ["recurring", "--end", "4", "--re-review", "5"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    assert calls == []


def test_recurring_table_shows_id_and_direction(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> int:
        return (await make_series(session)).id

    series_id = _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["recurring"])
    assert result.exit_code == 0
    assert "ID" in result.output
    assert str(series_id) in result.output
    assert "outflow" in result.output


def test_recurring_events_show_merchant_and_id(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> int:
        series = await make_series(session)
        await make_series_event(session, series)
        return series.id

    series_id = _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["recurring", "--events"])
    assert result.exit_code == 0
    assert "Netflix" in result.output
    # two series can share a merchant name — the ID column disambiguates and feeds --end
    assert "ID" in result.output
    assert str(series_id) in result.output


def test_tables_survive_markup_hostile_merchant_names(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        series = await make_series(session, merchant="WEIRD [/bold] CO")
        await make_series_event(session, series)

    _seed_db(tmp_path, _seed)
    for args in (["recurring"], ["recurring", "--events"], ["power"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"
        assert "WEIRD" in result.output


def test_cashflow_runs_in_process(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["cashflow"])
    assert result.exit_code == 0
    assert "Cashflow" in result.output


def test_cashflow_date_flags_pass_params(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, params))
        return {
            "start": "2026-01-01",
            "end": "2026-06-30",
            "accrual_cents": 1234,
            "cash_out_cents": 500,
        }

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["cashflow", "--start", "2026-01-01", "--end", "2026-06-30"])
    assert result.exit_code == 0
    assert calls == [("GET", "/cashflow", {"start": "2026-01-01", "end": "2026-06-30"})]
    assert "$12.34" in result.output


def test_cashflow_invalid_date_fails_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["cashflow", "--start", "not-a-date"])
    assert result.exit_code == 1
    assert "invalid date" in result.output
    assert "Traceback" not in result.output


def test_txns_table_renders_counted_and_excluded_rows(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    today = date.today()

    async def _seed(session: AsyncSession) -> None:
        checking = await make_account(session, type=AccountType.checking)
        await make_txn(
            session, checking, amount_cents=-2500, merchant="Coffee Shop", posted_on=today
        )
        await make_txn(
            session, checking, amount_cents=300000, merchant="Acme Payroll", posted_on=today
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["txns"])
    assert result.exit_code == 0
    assert "Coffee Shop" in result.output
    assert "Acme Payroll" in result.output
    assert "✓" in result.output
    assert "inflow" in result.output
    assert "Counted as spend: -$25.00" in result.output
    assert "(power's spent-so-far for this range)" not in result.output
    assert "Through today (power's spent-so-far): -$25.00" in result.output


def test_txns_footer_drops_power_parity_claim_when_filtered(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    today = date.today()

    async def _seed(session: AsyncSession) -> None:
        checking = await make_account(session, type=AccountType.checking)
        await make_txn(
            session, checking, amount_cents=-2500, merchant="Coffee Shop", posted_on=today
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["txns", "--merchant", "Coffee"])
    assert result.exit_code == 0
    assert "Counted as spend: -$25.00" in result.output
    assert "Through today: -$25.00" in result.output
    assert "power's spent-so-far" not in result.output


def test_txns_month_and_start_are_mutually_exclusive(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["txns", "--month", "2026-07", "--start", "2026-07-01"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    assert "Traceback" not in result.output


def test_txns_filters_pass_through_as_query_params(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, params))
        return {
            "start": "2026-01-01",
            "end": "2026-01-31",
            "counted_total_cents": 0,
            "through_today_cents": None,
            "transactions": [],
        }

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(
        app,
        [
            "txns",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-31",
            "--account",
            "3",
            "--merchant",
            "netflix",
        ],
    )
    assert result.exit_code == 0
    assert calls == [
        (
            "GET",
            "/transactions",
            {
                "start": "2026-01-01",
                "end": "2026-01-31",
                "account_id": 3,
                "merchant": "netflix",
            },
        )
    ]


def test_txns_month_flag_expands_to_full_month(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, params))
        return {
            "start": "2026-02-01",
            "end": "2026-02-28",
            "counted_total_cents": 0,
            "through_today_cents": None,
            "transactions": [],
        }

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["txns", "--month", "2026-02"])
    assert result.exit_code == 0
    assert calls == [("GET", "/transactions", {"start": "2026-02-01", "end": "2026-02-28"})]


def test_txns_invalid_month_fails_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["txns", "--month", "not-a-month"])
    assert result.exit_code == 1
    assert "invalid month" in result.output
    assert "Traceback" not in result.output


def test_txns_footer_sums_only_counted_rows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return {
            "start": "2026-07-01",
            "end": "2026-07-31",
            "counted_total_cents": 2500,
            "through_today_cents": 2500,
            "transactions": [
                {
                    "id": 1,
                    "posted_on": "2026-07-05",
                    "account": "Checking",
                    "account_type": "checking",
                    "merchant": "Coffee Shop",
                    "description": "COFFEE SHOP",
                    "amount_cents": -2500,
                    "series": None,
                    "series_status": None,
                    "series_discretionary": None,
                    "link": None,
                    "counted_in_spend": True,
                    "excluded_because": None,
                },
                {
                    "id": 2,
                    "posted_on": "2026-07-05",
                    "account": "Checking",
                    "account_type": "checking",
                    "merchant": "Netflix",
                    "description": "NETFLIX",
                    "amount_cents": -1599,
                    "series": "Netflix",
                    "series_status": "active",
                    "series_discretionary": False,
                    "link": None,
                    "counted_in_spend": False,
                    "excluded_because": "fixed cost (series Netflix)",
                },
            ],
        }

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["txns"])
    assert result.exit_code == 0
    assert "fixed cost (series Netflix)" in result.output
    assert "Counted as spend: -$25.00" in result.output  # only the counted row
    assert "Through today (power's spent-so-far): -$25.00" in result.output


def test_txns_footer_line2_includes_only_todays_counted_rows(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    today = date.today()
    last_day = monthrange(today.year, today.month)[1]
    future_day = today + timedelta(days=1)
    if future_day > date(today.year, today.month, last_day):
        pytest.skip("today is the last day of the month; no room for a future txn this month")

    async def _seed(session: AsyncSession) -> None:
        checking = await make_account(session, type=AccountType.checking)
        await make_txn(
            session, checking, amount_cents=-2500, merchant="Coffee Shop", posted_on=today
        )
        await make_txn(
            session, checking, amount_cents=-900, merchant="Future Charge", posted_on=future_day
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["txns"])
    assert result.exit_code == 0
    assert "Counted as spend: -$34.00" in result.output  # both counted rows
    assert "Through today (power's spent-so-far): -$25.00" in result.output  # today's only


def test_txns_footer_line2_absent_for_past_month_range(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    today = date.today()
    if today.month == 1:
        last_month_year, last_month = today.year - 1, 12
    else:
        last_month_year, last_month = today.year, today.month - 1
    last_month_day = date(last_month_year, last_month, 15)

    async def _seed(session: AsyncSession) -> None:
        checking = await make_account(session, type=AccountType.checking)
        await make_txn(
            session, checking, amount_cents=-1000, merchant="Old Coffee", posted_on=last_month_day
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["txns", "--month", f"{last_month_year:04d}-{last_month:02d}"])
    assert result.exit_code == 0
    assert "Counted as spend: -$10.00" in result.output
    assert "Through today" not in result.output


def test_power_itemizes_income_sources(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_series(
            session, merchant="Acme Payroll", direction=Direction.inflow, expected_cents=250000
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    assert "Acme Payroll" in result.output


def test_power_negative_money_renders_minus_before_dollar(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_series(session, merchant="Rent", expected_cents=-500000)

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    assert "-$5000.00" in result.output  # fixed costs / spending power / remaining
    assert "$-" not in result.output  # one sign format everywhere


def test_power_biweekly_renders_per_cycle_and_monthly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_series(
            session,
            merchant="Acme Payroll",
            direction=Direction.inflow,
            cadence=Cadence.biweekly,
            expected_cents=250000,
        )
        await make_series(session, merchant="Netflix", expected_cents=-1599)

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    assert "$2500.00 every 2 weeks ≈ $5416.67/mo" in result.output
    assert "$15.99" in result.output  # monthly row stays bare
    assert "(monthly)" not in result.output
    assert "(biweekly)" not in result.output


def test_power_per_day_row_after_remaining(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    today = date.today()
    last_day = monthrange(today.year, today.month)[1]
    days_left = (date(today.year, today.month, last_day) - today).days + 1
    assert f"Per day ({days_left} days left)" in result.output
    remaining_idx = result.output.index("Remaining")
    per_day_idx = result.output.index("Per day")
    assert per_day_idx > remaining_idx


def test_power_upcoming_charges_absent_when_empty(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    assert "Upcoming this month" not in result.output


def test_power_upcoming_charges_render_dim_line(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    today = date.today()
    last_day = monthrange(today.year, today.month)[1]
    month_end = date(today.year, today.month, last_day)
    expected_on = today + timedelta(days=1)
    if expected_on > month_end:
        pytest.skip("today is the last day of the month; no room for an upcoming charge")

    async def _seed(session: AsyncSession) -> None:
        await make_series(
            session, merchant="Rent", expected_cents=-140000, next_expected_on=expected_on
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["power"])
    assert result.exit_code == 0
    label = f"{expected_on.strftime('%b')} {expected_on.day}"
    assert f"Upcoming this month: Rent $1400.00 ({label})" in result.output


def test_power_history_runs_in_process(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["power", "--history", "3"])
    assert result.exit_code == 0
    assert "Month" in result.output
    assert "Spending power" not in result.output  # --history replaces the normal view


def test_power_history_table_renders_month_income_spend_net(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        assert (method, path, params) == ("GET", "/power/history", {"months": 3})
        return [
            {
                "month": "2026-07",
                "income_cents": 300000,
                "spend_cents": 5000,
                "net_cents": 295000,
            },
            {
                "month": "2026-06",
                "income_cents": 250000,
                "spend_cents": 260000,
                "net_cents": -10000,
            },
            {"month": "2026-05", "income_cents": 250000, "spend_cents": 4000, "net_cents": 246000},
        ]

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["power", "--history", "3"])
    assert result.exit_code == 0
    for header in ("Month", "Income", "Spend", "Net"):
        assert header in result.output
    assert "2026-07" in result.output
    assert "$3000.00" in result.output  # income: fmt_money
    assert "-$50.00" in result.output  # spend: fmt_outflow(5000)
    assert "$2950.00" in result.output  # net: fmt_money, positive
    assert "-$100.00" in result.output  # net: fmt_money, negative month
    assert "Spending power" not in result.output


def test_power_history_and_json_prints_history(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        assert (method, path, params) == ("GET", "/power/history", {"months": 1})
        return [
            {"month": "2026-07", "income_cents": 300000, "spend_cents": 5000, "net_cents": 295000}
        ]

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["power", "--history", "1", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body == [
        {"month": "2026-07", "income_cents": 300000, "spend_cents": 5000, "net_cents": 295000}
    ]
    assert "│" not in result.stdout


def test_review_non_integer_answer_skips_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        session.add(
            ReviewItem(
                kind="transfer_pair",
                question="Which inflow matches outflow 1?",
                payload={"outflow_id": 1, "candidates": [2]},
            )
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["review"], input="abc\n")
    assert result.exit_code == 0
    assert "skipping" in result.output
    assert "Traceback" not in result.output


def _seed_recurring_cluster_review(tmp_path: Path) -> None:
    async def _seed(session: AsyncSession) -> None:
        session.add(
            ReviewItem(
                kind="recurring_cluster",
                question="Is 'Util Co' a recurring bill?",
                payload={"merchant": "Util Co", "direction": "outflow"},
            )
        )

    _seed_db(tmp_path, _seed)


def test_review_recurring_cluster_yes_resolves(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_recurring_cluster_review(tmp_path)
    result = runner.invoke(app, ["review"], input="y\n")
    assert result.exit_code == 0
    assert "Bill, habit, or not recurring? [b/h/n]" in result.output
    assert "resolved" in result.output
    assert "Traceback" not in result.output


def test_review_recurring_three_way(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The b/h/n prompt: 'h' resolves as a discretionary recurring habit."""
    calls: list[tuple[str, str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, path, json_body))
        if path == "/review":
            return [
                {
                    "id": 7,
                    "kind": "recurring_cluster",
                    "question": "Is 'Util Co' a recurring bill?",
                    "payload": {"merchant": "Util Co", "direction": "outflow"},
                    "context": {"samples": [], "direction": "outflow"},
                }
            ]
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["review"], input="h\n")
    assert result.exit_code == 0
    assert (
        "POST",
        "/review/7/resolve",
        {"resolution": {"is_recurring": True, "discretionary": True}},
    ) in calls
    assert "Traceback" not in result.output


def test_review_recurring_cluster_shows_llm_leaning(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A verify_series-flagged item carries payload.llm_leaning; the CLI surfaces it
    before the b/h/n prompt so a human reviewing knows what the LLM already thought."""

    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if path == "/review":
            return [
                {
                    "id": 7,
                    "kind": "recurring_cluster",
                    "question": "Is 'Util Co' a recurring bill?",
                    "payload": {
                        "merchant": "Util Co",
                        "direction": "outflow",
                        "llm_flagged": True,
                        "llm_leaning": "habit",
                    },
                    "context": {"samples": [], "direction": "outflow"},
                }
            ]
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["review"], input="\n")
    assert result.exit_code == 0
    assert "LLM leaned: habit" in result.output
    assert "Traceback" not in result.output


def test_review_recurring_cluster_invalid_answer_skips_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_recurring_cluster_review(tmp_path)
    result = runner.invoke(app, ["review"], input="maybe\n")
    assert result.exit_code == 0
    assert "invalid input, skipping" in result.output
    assert "Traceback" not in result.output


def test_review_shows_summary_and_numbered_candidates(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        checking = await make_account(session, type=AccountType.checking)
        savings = await make_account(session, type=AccountType.savings, name="My Savings")
        out = await make_txn(
            session,
            checking,
            amount_cents=-50000,
            posted_on=date(2026, 7, 1),
            description="ACH TRANSFER",
        )
        c1 = await make_txn(
            session,
            savings,
            amount_cents=50000,
            posted_on=date(2026, 7, 2),
            description="DEPOSIT A",
        )
        session.add(
            ReviewItem(
                kind="transfer_pair",
                question="Which inflow matches?",
                payload={"outflow_id": out.id, "candidates": [c1.id]},
            )
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["review"], input="1\n")
    assert result.exit_code == 0
    assert "Review queue" in result.output  # upfront summary
    assert "transfer match" in result.output  # kind explained
    assert "DEPOSIT A" in result.output  # candidate rendered, not a raw id
    assert "resolved" in result.output
    assert "Traceback" not in result.output


def test_review_summary_counts_kinds(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_recurring_cluster_review(tmp_path)
    result = runner.invoke(app, ["review"], input="\n")
    assert result.exit_code == 0
    assert "Review queue" in result.output
    assert "bill or habit" in result.output
    assert "skipped" in result.output.lower()


def test_renormalize_command_runs(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["renormalize"])
    assert result.exit_code == 0
    assert "Updated 0 merchant name(s)" in result.output


def test_sync_prints_verification_line(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if path == "/sync":
            return {
                "ingest": {"new_transactions": 0},
                "transfers": {"linked": 0},
                "recurring": {"new_series": 1},
                "events": 0,
                "auto_resolved": 0,
                "verify": {"verified": 2, "flagged": 1},
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "LLM verified 2 series; flagged 1 for review." in result.output


def test_sync_prints_warnings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if path == "/sync":
            return {
                "ingest": {"new_transactions": 0},
                "transfers": {"linked": 0},
                "recurring": {"new_series": 0},
                "events": 0,
                "auto_resolved": 0,
                "verify": {"verified": 0, "flagged": 0},
                "warnings": [
                    "simplefin: bridge error: re-authenticate at the institution",
                    "Plaid item Old Bank skipped (ITEM_LOGIN_REQUIRED: re-link)"
                    " — re-link with: moneta setup plaid-link",
                ],
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "⚠ simplefin: bridge error: re-authenticate at the institution" in result.output
    assert "⚠ Plaid item Old Bank skipped" in result.output


def test_sync_omits_warnings_section_when_healthy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if path == "/sync":
            return {
                "ingest": {"new_transactions": 0},
                "transfers": {"linked": 0},
                "recurring": {"new_series": 0},
                "events": 0,
                "auto_resolved": 0,
                "verify": {"verified": 0, "flagged": 0},
                "warnings": [],
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "⚠" not in result.output


def _seed_price_change_review(tmp_path: Path) -> None:
    async def _seed() -> None:
        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            series = await make_series(session)
            session.add(make_price_change_item(series.id))
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())


def test_review_price_change_yes_resolves(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_price_change_review(tmp_path)
    result = runner.invoke(app, ["review"], input="y\n")
    assert result.exit_code == 0
    assert "$15.99 → $18.99" in result.output
    assert "Price change? [y/n]" in result.output
    assert "resolved" in result.output
    assert "Traceback" not in result.output


def test_review_price_change_invalid_answer_skips_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_price_change_review(tmp_path)
    result = runner.invoke(app, ["review"], input="maybe\n")
    assert result.exit_code == 0
    assert "invalid input, skipping" in result.output
    assert "Traceback" not in result.output


def _setup_plaid(tmp_path: Path, monkeypatch) -> Any:  # type: ignore[no-untyped-def]
    """Isolate env, save sandbox Plaid credentials, return the plaid module."""
    import moneta.aggregator.plaid as plaid_mod

    _isolate(monkeypatch, tmp_path)
    runner.invoke(app, ["setup", "plaid", "cid", "sec", "--env", "sandbox"])
    return plaid_mod


def test_setup_plaid_saves_credentials(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["setup", "plaid", "cid", "sec", "--env", "sandbox"])
    assert result.exit_code == 0
    assert "plaid-link" in result.output
    from moneta.config import load_settings

    s = load_settings()
    assert (s.plaid_client_id, s.plaid_secret, s.plaid_env) == ("cid", "sec", "sandbox")


def test_setup_plaid_rejects_bad_env(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["setup", "plaid", "cid", "sec", "--env", "development"])
    assert result.exit_code == 1
    assert "production" in result.output


def test_setup_plaid_link_requires_credentials(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["setup", "plaid-link"])
    assert result.exit_code == 1
    assert "moneta setup plaid" in result.output


def test_setup_plaid_link_happy_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    plaid_mod = _setup_plaid(tmp_path, monkeypatch)

    async def fake_create(client: Any, products: list[str]) -> Any:
        return "lt-1", "https://hosted.plaid.com/link/abc"

    async def fake_poll(
        client: Any, link_token: str, timeout: float = 900.0, interval: float = 3.0
    ) -> Any:
        assert link_token == "lt-1"
        return "public-1", "Chase"

    async def fake_exchange(client: Any, public_token: str) -> Any:
        assert public_token == "public-1"
        return "access-1", "it-1"

    monkeypatch.setattr(plaid_mod, "create_hosted_link", fake_create)
    monkeypatch.setattr(plaid_mod, "poll_link_result", fake_poll)
    monkeypatch.setattr(plaid_mod, "exchange_public_token", fake_exchange)

    result = runner.invoke(app, ["setup", "plaid-link"])
    assert result.exit_code == 0
    assert "hosted.plaid.com" in result.output
    assert "Linked Chase" in result.output

    items = plaid_mod.load_items(plaid_mod.items_path(tmp_path))
    assert len(items) == 1
    assert items[0].item_id == "it-1"
    assert items[0].access_token == "access-1"
    assert items[0].products == ["transactions"]


def test_setup_plaid_list_and_unlink(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    plaid_mod = _setup_plaid(tmp_path, monkeypatch)
    plaid_mod.save_items(
        plaid_mod.items_path(tmp_path),
        [plaid_mod.PlaidItem(item_id="it-1", access_token="a", institution_name="Chase")],
    )

    result = runner.invoke(app, ["setup", "plaid-list"])
    assert result.exit_code == 0
    assert "Chase" in result.output

    removed: list[str] = []

    async def fake_post(self: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        removed.append(path)
        return {"request_id": "r"}

    monkeypatch.setattr(plaid_mod.PlaidClient, "post", fake_post)
    result = runner.invoke(app, ["setup", "plaid-unlink", "it-1"])
    assert result.exit_code == 0
    assert removed == ["/item/remove"]
    assert plaid_mod.load_items(plaid_mod.items_path(tmp_path)) == []

    result = runner.invoke(app, ["setup", "plaid-unlink", "nope"])
    assert result.exit_code == 1
    assert "plaid-list" in result.output


def test_setup_plaid_unlink_survives_remote_failure(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    plaid_mod = _setup_plaid(tmp_path, monkeypatch)
    plaid_mod.save_items(
        plaid_mod.items_path(tmp_path),
        [plaid_mod.PlaidItem(item_id="it-dead", access_token="a", institution_name="Old Bank")],
    )

    async def failing_post(self: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise plaid_mod.PlaidError("ITEM_ERROR", "ITEM_NOT_FOUND", "already gone")

    monkeypatch.setattr(plaid_mod.PlaidClient, "post", failing_post)
    result = runner.invoke(app, ["setup", "plaid-unlink", "it-dead"])
    assert result.exit_code == 0
    assert "removing locally" in result.output
    assert plaid_mod.load_items(plaid_mod.items_path(tmp_path)) == []


def test_status_before_any_sync(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No sync has run yet" in result.output


def test_status_shows_in_flight_sync_as_incomplete(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def seed(session: AsyncSession) -> None:
        session.add(SyncRun())  # as run_sync writes it before the stages run

    _seed_db(tmp_path, seed)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "incomplete" in result.output
    assert "failed" not in result.output


def test_serve_refuses_public_bind_without_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    import uvicorn

    called: list[int] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(1))
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "token" in result.output.lower()
    assert not called


def test_serve_public_bind_allowed_with_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("MONETA_API_TOKEN", "t0k3n")
    import uvicorn

    called: list[int] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(1))
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0
    assert called


def test_in_process_cli_works_with_token_configured(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text('api_token = "t0k3n"\n')
    result = runner.invoke(app, ["networth"])  # client must attach the bearer header
    assert result.exit_code == 0


def test_backup_command(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    runner.invoke(app, ["networth"])  # forces DB creation
    dest = tmp_path / "snap.db"
    result = runner.invoke(app, ["backup", str(dest)])
    assert result.exit_code == 0
    assert dest.exists()


def test_obligations_renders_deferred_interest_warning(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    # /obligations resolves date.today() at request time — seed relative dates
    today = date.today()

    async def seed(session: AsyncSession) -> None:
        checking = await make_account(session, type=AccountType.checking)
        loan = await make_account(
            session,
            type=AccountType.loan,
            name="Synchrony CarCare",
            balance_cents=-121500,
            promo_expires_on=today + timedelta(days=60),
        )
        series = await make_series(
            session,
            merchant="Synchrony Bank",
            expected_cents=-13500,
            next_expected_on=today + timedelta(days=27),
        )
        for days_ago in (65, 35, 4):
            out = await make_txn(
                session,
                checking,
                amount_cents=-13500,
                merchant="Synchrony Bank",
                posted_on=today - timedelta(days=days_ago),
                series_id=series.id,
            )
            inn = await make_txn(
                session,
                loan,
                amount_cents=13500,
                merchant="Synchrony Bank",
                posted_on=today - timedelta(days=days_ago),
            )
            session.add(
                TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
            )

    _seed_db(tmp_path, seed)
    result = runner.invoke(app, ["obligations"])
    assert result.exit_code == 0
    assert "Synchrony" in result.output  # rich may wrap the full name across cell lines
    assert "deferred interest" in result.output  # payoff ~9mo out > promo ~2mo out
    assert "Traceback" not in result.output


def test_recurring_events_flag_renders_table(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def seed(session: AsyncSession) -> None:
        series = await make_series(session)
        session.add(
            SeriesEvent(
                series_id=series.id,
                kind=EventKind.missed,
                occurred_on=date(2026, 6, 15),
                details={"expected_on": "2026-06-15"},
            )
        )

    _seed_db(tmp_path, seed)
    result = runner.invoke(app, ["recurring", "--events"])
    assert result.exit_code == 0
    assert "missed" in result.output
    assert "Traceback" not in result.output


def test_setup_simplefin_saves_access_url(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def fake_claim(token: str) -> str:
        assert token == "TOKEN"
        return "https://u:p@bridge.example/simplefin"

    monkeypatch.setattr("moneta.aggregator.simplefin.claim_setup_token", fake_claim)
    result = runner.invoke(app, ["setup", "simplefin", "TOKEN"])
    assert result.exit_code == 0
    assert "connected" in result.output.lower()
    from moneta.config import load_settings

    assert load_settings().simplefin_access_url == "https://u:p@bridge.example/simplefin"


def test_fmt_money_formats_cents() -> None:
    from moneta.cli.main import fmt_money

    assert fmt_money(0) == "$0.00"
    assert fmt_money(1599) == "$15.99"
    assert fmt_money(-3609) == "-$36.09"
    assert fmt_money(-5) == "-$0.05"
    assert fmt_money(123456789) == "$1234567.89"


def test_fmt_outflow_renders_magnitude_with_display_minus() -> None:
    from moneta.cli.main import fmt_outflow

    assert fmt_outflow(512242) == "-$5122.42"
    assert fmt_outflow(0) == "$0.00"


def test_power_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["power", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert "remaining_cents" in body


def test_networth_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["networth", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert "net_worth_cents" in body


def test_cashflow_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["cashflow", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert "accrual_cents" in body


def test_recurring_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_series(session, merchant="Netflix")

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["recurring", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert isinstance(body, list)
    assert body[0]["merchant"] == "Netflix"


def test_recurring_events_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        series = await make_series(session)
        await make_series_event(session, series)

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["recurring", "--events", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert isinstance(body, list)
    assert body[0]["kind"] == "missed"


def test_obligations_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_account(session, type=AccountType.loan, balance_cents=-121500)

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["obligations", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert isinstance(body, list)
    assert "balance_owed_cents" in body[0]


def test_accounts_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_account(session, name="Checking")

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["accounts", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert isinstance(body, list)
    assert body[0]["name"] == "Checking"


def test_txns_json_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    today = date.today()

    async def _seed(session: AsyncSession) -> None:
        checking = await make_account(session, type=AccountType.checking)
        await make_txn(
            session, checking, amount_cents=-2500, merchant="Coffee Shop", posted_on=today
        )

    _seed_db(tmp_path, _seed)
    result = runner.invoke(app, ["txns", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert isinstance(body["transactions"], list)
    assert "counted_in_spend" in body["transactions"][0]
    assert body["counted_total_cents"] == 2500
    assert body["through_today_cents"] == 2500


def test_status_json_output_null_before_any_sync(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "null"


def test_status_json_output_with_sync(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def seed(session: AsyncSession) -> None:
        session.add(SyncRun())

    _seed_db(tmp_path, seed)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["status"] == "incomplete"


def test_recurring_json_with_write_flag_errors_before_request(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        raise AssertionError("request must not be called when --json combines with a write flag")

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["recurring", "--end", "1", "--json"])
    assert result.exit_code == 1
    assert "Error" in result.output
    assert "Traceback" not in result.output


def test_accounts_json_with_write_flag_errors_before_request(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_request(
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        raise AssertionError("request must not be called when --json combines with a write flag")

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["accounts", "--set-type", "1", "checking", "--json"])
    assert result.exit_code == 1
    assert "Error" in result.output
    assert "Traceback" not in result.output


def test_json_output_has_no_rich_table_chars(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed(session: AsyncSession) -> None:
        await make_series(session, merchant="Netflix")
        checking = await make_account(session, type=AccountType.checking)
        await make_txn(session, checking, amount_cents=-2500, posted_on=date.today())

    _seed_db(tmp_path, _seed)
    for args in (
        ["power", "--json"],
        ["networth", "--json"],
        ["cashflow", "--json"],
        ["recurring", "--json"],
        ["obligations", "--json"],
        ["accounts", "--json"],
        ["txns", "--json"],
        ["status", "--json"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"
        assert "│" not in result.stdout, f"{args}: rich table leaked into --json output"
