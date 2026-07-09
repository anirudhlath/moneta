"""Loguru sink setup: warnings to stderr, everything to a rotating file."""

import sys
from pathlib import Path

from loguru import logger

from moneta.config import ensure_private_dir, make_private

_configured = False


def configure_logging(config_dir: Path) -> None:
    global _configured
    if _configured:  # build_app runs once per server but per-command in-process
        return
    _configured = True
    log_file = make_private(ensure_private_dir(config_dir) / "moneta.log")
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(log_file, rotation="10 MB", retention=5, level="INFO")
