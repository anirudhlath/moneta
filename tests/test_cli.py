import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from moneta.cli.main import app
from moneta.db import init_db, make_sessionmaker
from moneta.models import ReviewItem
from tests.factories import make_series

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
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        calls.append((method, path))
        if path.startswith("/sync"):
            return {
                "ingest": {"new_transactions": 0},
                "transfers": {"linked": 0},
                "recurring": {"new_series": 0},
                "events": 0,
            }
        return []

    monkeypatch.setattr("moneta.cli.main.request", fake_request)
    result = runner.invoke(app, ["sync", "--full"])
    assert result.exit_code == 0
    assert calls[0] == ("POST", "/sync?full=true")


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
