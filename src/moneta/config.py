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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONETA_", extra="ignore")

    config_dir: Path
    db_path: Path
    simplefin_access_url: str | None = None
    llm_model: str | None = None
    api_url: str | None = None


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
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)  # the config file holds bank credentials
    path = config_dir / "config.toml"
    values = _read_config_file(config_dir)
    values[key] = value
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(tomli_w.dumps(values))
    path.chmod(0o600)
