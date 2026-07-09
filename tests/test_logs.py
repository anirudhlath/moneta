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
    text = (tmp_path / "moneta.log").read_text()
    assert text.count("hello") == 1
