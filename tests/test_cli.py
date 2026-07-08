from pathlib import Path

from typer.testing import CliRunner

from moneta.cli.main import app

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


def test_import_vesting(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    csv_file = tmp_path / "vest.csv"
    csv_file.write_text("symbol,vested_quantity,unvested_quantity\nACME,40,60\n")
    result = runner.invoke(app, ["import", "vesting", str(csv_file)])
    assert result.exit_code == 0
    assert "0" in result.output  # updated count (no holdings in fresh db)
