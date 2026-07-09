from pathlib import Path

import pytest
from loguru import logger

import moneta.logs
from moneta.logs import configure_logging


def test_configure_logging_writes_file_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(moneta.logs, "_configured_dir", None)
    configure_logging(tmp_path)
    configure_logging(tmp_path)  # same dir again must not add duplicate sinks
    logger.info("hello")
    log_file = tmp_path / "moneta.log"
    assert log_file.read_text().count("hello") == 1
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_configure_logging_rebinds_to_a_new_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(moneta.logs, "_configured_dir", None)
    first = tmp_path / "a"
    second = tmp_path / "b"
    configure_logging(first)
    configure_logging(second)  # a different config dir must win, not silently no-op
    logger.info("rebound")
    assert "rebound" in (second / "moneta.log").read_text()
