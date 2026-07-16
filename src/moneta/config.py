"""Settings: env vars (MONETA_*) override a TOML config file in the config dir."""

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic_settings import BaseSettings, SettingsConfigDict


def _config_dir() -> Path:
    if env := os.environ.get("MONETA_CONFIG_DIR"):
        return Path(env)
    return Path.home() / ".config" / "moneta"


def ensure_private_dir(path: Path) -> Path:
    """Everything under the config dir is financial data — owner-only (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def make_private(file: Path) -> Path:
    """Owner-only (0600) file — credentials, database snapshots, logs."""
    file.touch(mode=0o600, exist_ok=True)
    file.chmod(0o600)
    return file


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONETA_", extra="ignore")

    config_dir: Path
    db_path: Path
    simplefin_access_url: str | None = None
    plaid_client_id: str | None = None
    plaid_secret: str | None = None
    plaid_env: str = "production"
    llm_model: str | None = None
    api_url: str | None = None
    api_token: str | None = None


def _read_config_file(config_dir: Path) -> dict[str, Any]:
    path = config_dir / "config.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def load_settings() -> Settings:
    config_dir = _config_dir()
    file_values = _read_config_file(config_dir)
    file_values.setdefault("db_path", config_dir / "moneta.db")
    for key in list(file_values):
        if f"MONETA_{key.upper()}" in os.environ:
            del file_values[key]
    return Settings(config_dir=config_dir, **file_values)


def save_config_value(key: str, value: str) -> None:
    config_dir = ensure_private_dir(_config_dir())  # the config file holds bank credentials
    values = _read_config_file(config_dir)
    values[key] = value
    make_private(config_dir / "config.toml").write_text(tomli_w.dumps(values))
