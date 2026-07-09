from pathlib import Path

import pytest
from loguru import logger

import moneta.logs
from moneta.logs import configure_logging


def test_configure_logging_writes_file_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(moneta.logs, "_configured", False)
    configure_logging(tmp_path)
    configure_logging(tmp_path)  # second call must not add duplicate sinks
    logger.info("hello")
    log_file = tmp_path / "moneta.log"
    assert log_file.read_text().count("hello") == 1
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
