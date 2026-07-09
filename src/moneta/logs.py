"""Loguru sink setup: warnings to stderr, everything to a rotating file."""

import sys
from pathlib import Path

from loguru import logger

_configured = False


def configure_logging(config_dir: Path) -> None:
    global _configured
    if _configured:  # build_app runs once per server but per-command in-process
        return
    _configured = True
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)  # sibling of config.toml/moneta.db — same treatment
    log_file = config_dir / "moneta.log"
    log_file.touch(mode=0o600, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(log_file, rotation="10 MB", retention=5, level="INFO")
