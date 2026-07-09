import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from moneta.cli.main import app
from moneta.db import init_db, make_sessionmaker
from moneta.models import ReviewItem
from tests.factories import make_price_change_item, make_series

runner = CliRunner()


def _isolate(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("MONETA_API_URL", raising=False)
    monkeypatch.delenv("MONETA_SIMPLEFIN_ACCESS_URL", raising=False)


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
    assert "SimpleFIN" in result.output


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


def test_recurring_end_option_ends_series(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed() -> int:
        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            series = await make_series(session, next_expected_on=date(2026, 7, 15))
            await session.commit()
            await session.refresh(series)
            series_id = series.id
        await engine.dispose()
        return series_id

    series_id = asyncio.run(_seed())
    result = runner.invoke(app, ["recurring", "--end", str(series_id)])
    assert result.exit_code == 0
    assert "ended" in result.output.lower()
    assert "Traceback" not in result.output

    result = runner.invoke(app, ["recurring"])
    assert "ended" in result.output


def test_review_non_integer_answer_skips_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)

    async def _seed() -> None:
        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            session.add(
                ReviewItem(
                    kind="transfer_pair",
                    question="Which inflow matches outflow 1?",
                    payload={"outflow_id": 1, "candidates": [2]},
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())
    result = runner.invoke(app, ["review"], input="abc\n")
    assert result.exit_code == 0
    assert "skipping" in result.output
    assert "Traceback" not in result.output


def _seed_recurring_cluster_review(tmp_path: Path) -> None:
    async def _seed() -> None:
        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            session.add(
                ReviewItem(
                    kind="recurring_cluster",
                    question="Is 'Util Co' a recurring bill?",
                    payload={"merchant": "Util Co", "direction": "outflow"},
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())


def test_review_recurring_cluster_yes_resolves(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    _seed_recurring_cluster_review(tmp_path)
    result = runner.invoke(app, ["review"], input="y\n")
    assert result.exit_code == 0
    assert "Recurring? [y/n]" in result.output
    assert "resolved" in result.output
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

    async def _seed() -> None:
        from datetime import date as _date

        from moneta.models import AccountType
        from tests.factories import make_account, make_txn

        engine, sessionmaker = make_sessionmaker(f"sqlite+aiosqlite:///{tmp_path / 'moneta.db'}")
        await init_db(engine)
        async with sessionmaker() as session:
            checking = await make_account(session, type=AccountType.checking)
            savings = await make_account(session, type=AccountType.savings, name="My Savings")
            out = await make_txn(
                session,
                checking,
                amount_cents=-50000,
                posted_on=_date(2026, 7, 1),
                description="ACH TRANSFER",
            )
            c1 = await make_txn(
                session,
                savings,
                amount_cents=50000,
                posted_on=_date(2026, 7, 2),
                description="DEPOSIT A",
            )
            session.add(
                ReviewItem(
                    kind="transfer_pair",
                    question="Which inflow matches?",
                    payload={"outflow_id": out.id, "candidates": [c1.id]},
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())
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
    assert "recurring bill" in result.output
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
